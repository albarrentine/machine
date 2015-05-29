from __future__ import absolute_import, division, print_function
import logging; _L = logging.getLogger('openaddr.cache')

from .compat import standard_library

import ogr
import osr
import sys
import os
import errno
import socket
import mimetypes
import shutil
import itertools
import re
import time

from os import mkdir
from hashlib import md5
from os.path import join, basename, exists, abspath, dirname, splitext
from urllib.parse import urlparse
from subprocess import check_output
from tempfile import mkstemp
from hashlib import sha1
from shutil import move

import requests
import requests_ftp
requests_ftp.monkeypatch_session()

# HTTP timeout in seconds, used in various calls to requests.get() and requests.post()
_http_timeout = 180

from .compat import csvopen, csvDictWriter
from .conform import X_FIELDNAME, Y_FIELDNAME, GEOM_FIELDNAME

def mkdirsp(path):
    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise


class CacheResult:
    cache = None
    fingerprint = None
    version = None
    elapsed = None

    def __init__(self, cache, fingerprint, version, elapsed):
        self.cache = cache
        self.fingerprint = fingerprint
        self.version = version
        self.elapsed = elapsed

    @staticmethod
    def empty():
        return CacheResult(None, None, None, None)

    def todict(self):
        return dict(cache=self.cache, fingerprint=self.fingerprint, version=self.version)


def compare_cache_details(filepath, resultdir, data):
    ''' Compare cache file with known source data, return cache and fingerprint.
    
        Checks if fresh data is already cached, returns a new file path if not.
    '''
    if not exists(filepath):
        raise Exception('cached file {} is missing'.format(filepath))
        
    fingerprint = md5()

    with open(filepath, 'rb') as file:
        for line in file:
            fingerprint.update(line)
    
    # Determine if anything needs to be done at all.
    if urlparse(data.get('cache', '')).scheme == 'http' and 'fingerprint' in data:
        if fingerprint.hexdigest() == data['fingerprint']:
            return data['cache'], data['fingerprint']
    
    cache_name = basename(filepath)

    if not exists(resultdir):
        mkdir(resultdir)

    move(filepath, join(resultdir, cache_name))
    data_cache = 'file://' + join(abspath(resultdir), cache_name)
    
    return data_cache, fingerprint.hexdigest()

class DownloadError(Exception):
    pass


class DownloadTask(object):

    def __init__(self, source_prefix):
        self.source_prefix = source_prefix

    @classmethod
    def from_type_string(clz, type_string, source_prefix=None):
        if type_string.lower() == 'http':
            return URLDownloadTask(source_prefix)
        elif type_string.lower() == 'ftp':
            return URLDownloadTask(source_prefix)
        elif type_string.lower() == 'esri':
            return EsriRestDownloadTask(source_prefix)
        else:
            raise KeyError("I don't know how to extract for type {}".format(type_string))

    def download(self, source_urls, workdir):
        raise NotImplementedError()

def guess_url_file_extension(url):
    ''' Get a filename extension for a URL using various hints.
    '''
    scheme, _, path, _, query, _ = urlparse(url)
    mimetypes.add_type('application/x-zip-compressed', '.zip', False)
    
    _, likely_ext = os.path.splitext(path)
    bad_extensions = '', '.cgi', '.php', '.aspx', '.asp', '.do'
    
    if not query and likely_ext not in bad_extensions:
        #
        # Trust simple URLs without meaningless filename extensions.
        #
        _L.debug('URL says "{}" for {}'.format(likely_ext, url))
        path_ext = likely_ext
    
    else:
        #
        # Get a dictionary of headers and a few bytes of content from the URL.
        #
        if scheme in ('http', 'https'):
            response = requests.get(url, stream=True, timeout=_http_timeout)
            content_chunk = next(response.iter_content(99))
            headers = response.headers
            response.close()
        elif scheme in ('file', ''):
            headers = dict()
            with open(path) as file:
                content_chunk = file.read(99)
        else:
            raise ValueError('Unknown scheme "{}": {}'.format(scheme, url))
    
        path_ext = False
        
        # Guess path extension from Content-Type header
        if 'content-type' in headers:
            content_type = headers['content-type'].split(';')[0]
            _L.debug('Content-Type says "{}" for {}'.format(content_type, url))
            path_ext = mimetypes.guess_extension(content_type, False)

            #
            # Uh-oh, see if Content-Disposition disagrees with Content-Type.
            # Socrata recently started using Content-Disposition instead
            # of normal response headers so it's no longer easy to identify
            # file type.
            #
            if 'content-disposition' in headers:
                pattern = r'attachment; filename=("?)(?P<filename>[^;]+)\1'
                match = re.match(pattern, headers['content-disposition'], re.I)
                if match:
                    _, attachment_ext = splitext(match.group('filename'))
                    if path_ext == attachment_ext:
                        _L.debug('Content-Disposition agrees: "{}"'.format(match.group('filename')))
                    else:
                        _L.debug('Content-Disposition disagrees: "{}"'.format(match.group('filename')))
                        path_ext = False
        
        if not path_ext:
            #
            # Headers didn't clearly define a known extension.
            # Instead, shell out to `file` to peek at the content.
            #
            mime_type = get_content_mimetype(content_chunk)
            _L.debug('file says "{}" for {}'.format(mime_type, url))
            path_ext = mimetypes.guess_extension(mime_type, False)
    
    return path_ext

def get_content_mimetype(chunk):
    ''' Get a mime-type for a short length of file content.
    '''
    handle, file = mkstemp()
    os.write(handle, chunk)
    os.close(handle)
    
    mime_type = check_output(('file', '--mime-type', '-b', file)).strip()
    os.remove(file)
    
    return mime_type.decode('utf-8')

class URLDownloadTask(DownloadTask):
    USER_AGENT = 'openaddresses-extract/1.0 (https://github.com/openaddresses/openaddresses)'
    CHUNK = 16 * 1024

    def get_file_path(self, url, dir_path):
        ''' Return a local file path in a directory for a URL.

            May need to fill in a filename extension based on HTTP Content-Type.
        '''
        scheme, host, path, _, _, _ = urlparse(url)
        path_base, _ = os.path.splitext(path)

        if self.source_prefix is None:
            # With no source prefix like "us-ca-oakland" use the name as given.
            name_base = os.path.basename(path_base)
        else:
            # With a source prefix, create a safe and unique filename with a hash.
            hash = sha1((host + path_base).encode('utf-8'))
            name_base = '{}-{}'.format(self.source_prefix, hash.hexdigest()[:8])
        
        path_ext = guess_url_file_extension(url)
        _L.debug('Guessed {}{} for {}'.format(name_base, path_ext, url))
    
        return os.path.join(dir_path, name_base + path_ext)

    def download(self, source_urls, workdir):
        output_files = []
        download_path = os.path.join(workdir, 'http')
        mkdirsp(download_path)

        for source_url in source_urls:
            file_path = self.get_file_path(source_url, download_path)

            # FIXME: For URLs with file:// scheme, simply copy the file
            # to the expected location so that os.path.exists() returns True.
            # Instead, implement a FileDownloadTask class?
            scheme, _, path, _, _, _ = urlparse(source_url)
            if scheme == 'file':
                shutil.copy(path, file_path)

            if os.path.exists(file_path):
                output_files.append(file_path)
                _L.debug("File exists %s", file_path)
                continue

            _L.info("Requesting %s", source_url)
            headers = {'User-Agent': self.USER_AGENT}

            try:
                resp = requests.get(source_url, headers=headers, stream=True, timeout=_http_timeout)
            except Exception as e:
                raise DownloadError("Could not connect to URL", e)

            if resp.status_code in range(400, 499):
                raise DownloadError('{} response from {}'.format(resp.status_code, source_url))
            
            size = 0
            with open(file_path, 'wb') as fp:
                for chunk in resp.iter_content(self.CHUNK):
                    size += len(chunk)
                    fp.write(chunk)

            output_files.append(file_path)

            _L.info("Downloaded %s bytes for file %s", size, file_path)

        return output_files


class EsriRestDownloadTask(DownloadTask):
    USER_AGENT = 'openaddresses-extract/1.0 (https://github.com/openaddresses/openaddresses)'

    def build_ogr_geometry(self, geom_type, esri_feature):
        if 'geometry' not in esri_feature:
            raise TypeError("No geometry for feature")

        if geom_type == 'esriGeometryPoint':
            geom = ogr.Geometry(ogr.wkbPoint)
            geom.AddPoint(esri_feature['geometry']['x'], esri_feature['geometry']['y'])
        elif geom_type == 'esriGeometryMultipoint':
            geom = ogr.Geometry(ogr.wkbMultiPoint)
            for point in esri_feature['geometry']['points']:
                pt = ogr.Geometry(ogr.wkbPoint)
                pt.AddPoint(point[0], point[1])
                geom.AddGeometry(pt)
        elif geom_type == 'esriGeometryPolygon':
            geom = ogr.Geometry(ogr.wkbPolygon)
            for esri_ring in esri_feature['geometry']['rings']:
                ring = ogr.Geometry(ogr.wkbLinearRing)
                for esri_pt in esri_ring:
                    ring.AddPoint(esri_pt[0], esri_pt[1])
                geom.AddGeometry(ring)
        elif geom_type == 'esriGeometryPolyline':
            geom = ogr.Geometry(ogr.wkbMultiLineString)
            for esri_ring in esri_feature['geometry']['paths']:
                line = ogr.Geometry(ogr.wkbLineString)
                for esri_pt in esri_ring:
                    line.AddPoint(esri_pt[0], esri_pt[1])
                geom.AddGeometry(line)
        else:
            raise KeyError("Don't know how to convert esri geometry type {}".format(geom_type))

        return geom

    def get_file_path(self, url, dir_path):
        ''' Return a local file path in a directory for a URL.
        '''
        _, host, path, _, _, _ = urlparse(url)
        hash, path_ext = sha1((host + path).encode('utf-8')), '.csv'

        # With no source prefix like "us-ca-oakland" use the host as a hint.
        name_base = '{}-{}'.format(self.source_prefix or host, hash.hexdigest()[:8])

        _L.debug('Downloading {} to {}{}'.format(path, name_base, path_ext))

        return os.path.join(dir_path, name_base + path_ext)

    def download(self, source_urls, workdir):
        output_files = []
        download_path = os.path.join(workdir, 'esri')
        mkdirsp(download_path)

        for source_url in source_urls:
            size = 0
            file_path = self.get_file_path(source_url, download_path)

            if os.path.exists(file_path):
                output_files.append(file_path)
                _L.debug("File exists %s", file_path)
                continue

            headers = {'User-Agent': self.USER_AGENT}

            # Get the fields
            query_args = {
                'f': 'json'
            }
            response = requests.get(source_url, params=query_args, headers=headers, timeout=_http_timeout)

            if response.status_code != 200:
                raise DownloadError('Could not retrieve field names from ESRI source: HTTP {} {}'.format(
                    response.status_code,
                    response.text
                ))

            metadata = response.json()

            error = metadata.get('error')
            if error:
                raise DownloadError("Problem querying ESRI field names: {}" .format(error['message']))
            if not metadata.get('fields'):
                raise DownloadError("No fields available in the source")

            field_names = [f['name'] for f in metadata['fields']]
            if X_FIELDNAME not in field_names:
                field_names.append(X_FIELDNAME)
            if Y_FIELDNAME not in field_names:
                field_names.append(Y_FIELDNAME)
            if GEOM_FIELDNAME not in field_names:
                field_names.append(GEOM_FIELDNAME)

            objectid_fieldname = None
            for field in metadata['fields']:
                if field['type'] == 'esriFieldTypeOID':
                    objectid_fieldname = field['name']
                    break

            if not objectid_fieldname:
                raise DownloadError("Could not find objectid field name")

            geometry_type = metadata.get('geometryType')
            if not geometry_type:
                raise DownloadError("Could not determine geometry type")

            max_record_count = metadata.get('maxRecordCount', 500)

            extent = metadata.get('extent')
            # Reproject the extent to EPSG:4326 cuz ESRI won't do it for us
            ogr_extent = ogr.Geometry(ogr.wkbPolygon)
            ring = ogr.Geometry(ogr.wkbLinearRing)
            ring.AddPoint(extent['xmin'], extent['ymin'])
            ring.AddPoint(extent['xmin'], extent['ymax'])
            ring.AddPoint(extent['xmax'], extent['ymax'])
            ring.AddPoint(extent['xmax'], extent['ymin'])
            ring.AddPoint(extent['xmin'], extent['ymin'])
            ogr_extent.AddGeometry(ring)
            source = osr.SpatialReference()
            source.ImportFromEPSG(extent['spatialReference']['wkid'])
            target = osr.SpatialReference()
            target.ImportFromEPSG(4326)
            transform = osr.CoordinateTransformation(source, target)
            ogr_extent.Transform(transform)
            (xmin, xmax, ymin, ymax) = ogr_extent.GetEnvelope()
            extent = {
                'xmin': xmin,
                'ymin': ymin,
                'xmax': xmax,
                'ymax': ymax,
            }

            import json

            # Use spatial queries to fetch the data
            def get_bbox(fetched_ids, bbox):
                query_args = {
                    'f': 'json',
                    'geometryType': 'esriGeometryEnvelope',
                    'geometry': ','.join([
                        str(bbox['xmin']),
                        str(bbox['ymin']),
                        str(bbox['xmax']),
                        str(bbox['ymax']),
                    ]),
                    'geometryPrecision': 7,
                    'returnGeometry': 'true',
                    'outSR': 4326,
                    'inSR': 4326,
                    'outFields': '*',
                }

                tries = 3
                while tries >= 0:
                    try:
                        response = requests.get(source_url + '/query', params=query_args, headers=headers, timeout=_http_timeout)
                        tries = tries - 1

                        # print(response.url)

                        if response.status_code != 200:
                            raise DownloadError('Could not retrieve data envelope: HTTP {} {}'.format(
                                response.status_code,
                                response.text
                            ))

                        try:
                            error = response.json().get('error')
                            if error:
                                raise DownloadError("Problem querying ESRI dataset: {}" .format(error['message']))
                        except Exception as e:
                            raise DownloadError("Problem parsing ESRI response", e)

                        break
                    except DownloadError as e:
                        _L.info("Retrying after download error", exc_info=True)
                        time.sleep(1.0)

                geometry_type = response.json().get('geometryType')
                features = response.json().get('features')

                print(json.dumps({
                    'type': 'Feature',
                    'properties': {
                        'count': len(features),
                    },
                    'geometry': {
                        'type': 'Polygon',
                        'coordinates': [[
                            [bbox['xmin'], bbox['ymin']],
                            [bbox['xmin'], bbox['ymax']],
                            [bbox['xmax'], bbox['ymax']],
                            [bbox['xmax'], bbox['ymin']],
                            [bbox['xmin'], bbox['ymin']],
                        ]]
                    }
                }) + ',')

                returned_data = []
                for feature in features:
                    row = feature.get('attributes', {})

                    if row[objectid_fieldname] in fetched_ids:
                        continue

                    ogr_geom = self.build_ogr_geometry(geometry_type, feature)
                    try:
                        centroid = ogr_geom.Centroid()
                    except RuntimeError as e:
                        if 'Invalid number of points in LinearRing found' not in str(e):
                            raise
                        xmin, xmax, ymin, ymax = ogr_geom.GetEnvelope()
                        row[X_FIELDNAME] = round(xmin/2 + xmax/2, 7)
                        row[Y_FIELDNAME] = round(ymin/2 + ymax/2, 7)
                    else:
                        row[X_FIELDNAME] = round(centroid.GetX(), 7)
                        row[Y_FIELDNAME] = round(centroid.GetY(), 7)

                    fetched_ids.add(row[objectid_fieldname])
                    returned_data.append(row)

                if len(features) >= max_record_count:
                    # Use the mean x/y of the data we *did* get as the pivot point to split into 4 boxes
                    mean_x = float(sum(f[X_FIELDNAME] for f in returned_data)) / len(returned_data)
                    mean_y = float(sum(f[Y_FIELDNAME] for f in returned_data)) / len(returned_data)

                    returned_data.extend(
                        get_bbox(fetched_ids, {
                            'xmin': bbox['xmin'], 'ymin': bbox['ymin'],
                            'xmax': mean_x,       'ymax': mean_y,
                        })
                    )
                    returned_data.extend(
                        get_bbox(fetched_ids, {
                            'xmin': mean_x,       'ymin': bbox['ymin'],
                            'xmax': bbox['xmax'], 'ymax': mean_y,
                        })
                    )
                    returned_data.extend(
                        get_bbox(fetched_ids, {
                            'xmin': bbox['xmin'], 'ymin': mean_y,
                            'xmax': mean_x,       'ymax': bbox['ymax'],
                        })
                    )
                    returned_data.extend(
                        get_bbox(fetched_ids, {
                            'xmin': mean_x,       'ymin': mean_y,
                            'xmax': bbox['xmax'], 'ymax': bbox['ymax'],
                        })
                    )

                return returned_data

            with csvopen(file_path, 'w', encoding='utf-8') as f:
                writer = csvDictWriter(f, fieldnames=field_names, encoding='utf-8')
                writer.writeheader()

                fetched_ids = set()
                fetched_data = get_bbox(fetched_ids, extent)

                for feature in fetched_data:
                    try:
                        ogr_geom = self.build_ogr_geometry(geometry_type, feature)
                        row = feature.get('attributes', {})
                        row[GEOM_FIELDNAME] = ogr_geom.ExportToWkt()
                        try:
                            centroid = ogr_geom.Centroid()
                        except RuntimeError as e:
                            if 'Invalid number of points in LinearRing found' not in str(e):
                                raise
                            xmin, xmax, ymin, ymax = ogr_geom.GetEnvelope()
                            row[X_FIELDNAME] = round(xmin/2 + xmax/2, 7)
                            row[Y_FIELDNAME] = round(ymin/2 + ymax/2, 7)
                        else:
                            row[X_FIELDNAME] = round(centroid.GetX(), 7)
                            row[Y_FIELDNAME] = round(centroid.GetY(), 7)

                        writer.writerow(row)
                        size += 1
                    except TypeError:
                        _L.debug("Skipping a geometry", exc_info=True)

            _L.info("Downloaded %s ESRI features for file %s", size, file_path)
            output_files.append(file_path)
        return output_files
