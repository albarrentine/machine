from __future__ import absolute_import, division, print_function
import logging; _L = logging.getLogger('openaddr')

from tempfile import mkdtemp, mkstemp
from os.path import realpath, join, basename, splitext, exists, dirname, abspath, relpath
from shutil import copy, move, rmtree
from os import close, utime, remove
from urllib.parse import urlparse
from datetime import datetime, date
from calendar import timegm
import json

from osgeo import ogr
from requests import get
from boto.s3.connection import S3Connection
from dateutil.parser import parse
from .sample import sample_geojson

from .cache import (
    CacheResult,
    compare_cache_details,
    DownloadTask,
    URLDownloadTask,
)

from .conform import (
    ConformResult,
    DecompressionTask,
    ExcerptDataTask,
    ConvertToCsvTask,
    elaborate_filenames,
    conform_license,
    conform_attribution,
    conform_sharealike,
)

with open(join(dirname(__file__), 'VERSION')) as file:
    __version__ = file.read().strip()

class S3:
    _bucket = None

    def __init__(self, key, secret, bucketname):
        self._key, self._secret = key, secret
        self.bucketname = bucketname
    
    def _make_bucket(self):
        if not self._bucket:
            # see https://github.com/boto/boto/issues/2836#issuecomment-67896932
            kwargs = dict(calling_format='boto.s3.connection.OrdinaryCallingFormat')
            connection = S3Connection(self._key, self._secret, **kwargs)
            self._bucket = connection.get_bucket(self.bucketname)
    
    @property
    def bucket(self):
        self._make_bucket()
        return self._bucket
    
    def get_key(self, name):
        return self.bucket.get_key(name)
    
    def new_key(self, name):
        return self.bucket.new_key(name)

class LocalProcessedResult:
    def __init__(self, source_base, filename, run_state, code_version):
        for attr in ('attribution_name', 'attribution_flag', 'website', 'license'):
            assert hasattr(run_state, attr), 'Run state should have {} property'.format(attr)
        
        self.source_base = source_base
        self.filename = filename
        self.run_state = run_state
        self.code_version = code_version

def cache(srcjson, destdir, extras):
    ''' Python wrapper for openaddress-cache.
    
        Return a CacheResult object:

          cache: URL of cached data, possibly with file:// schema
          fingerprint: md5 hash of data,
          version: data version as date?
          elapsed: elapsed time as timedelta object
          output: subprocess output as string
        
        Creates and destroys a subdirectory in destdir.
    '''
    start = datetime.now()
    source, _ = splitext(basename(srcjson))
    workdir = mkdtemp(prefix='cache-', dir=destdir)
    
    with open(srcjson, 'r') as src_file:
        data = json.load(src_file)
        data.update(extras)
    
    #
    #
    #
    source_urls = data.get('data')
    if not isinstance(source_urls, list):
        source_urls = [source_urls]

    task = DownloadTask.from_type_string(data.get('type'), source)
    downloaded_files = task.download(source_urls, workdir, data.get('conform'))

    # FIXME: I wrote the download stuff to assume multiple files because
    # sometimes a Shapefile fileset is splayed across multiple files instead
    # of zipped up nicely. When the downloader downloads multiple files,
    # we should zip them together before uploading to S3 instead of picking
    # the first one only.
    filepath_to_upload = abspath(downloaded_files[0])
    
    #
    # Find the cached data and hold on to it.
    #
    resultdir = join(destdir, 'cached')
    data['cache'], data['fingerprint'] \
        = compare_cache_details(filepath_to_upload, resultdir, data)

    rmtree(workdir)

    return CacheResult(data.get('cache', None),
                       data.get('fingerprint', None),
                       data.get('version', None),
                       datetime.now() - start)

def conform(srcjson, destdir, extras):
    ''' Python wrapper for openaddresses-conform.
    
        Return a ConformResult object:

          processed: URL of processed data CSV
          path: local path to CSV of processed data
          geometry_type: typically Point or Polygon
          elapsed: elapsed time as timedelta object
          output: subprocess output as string
        
        Creates and destroys a subdirectory in destdir.
    '''
    start = datetime.now()
    source, _ = splitext(basename(srcjson))
    workdir = mkdtemp(prefix='conform-', dir=destdir)
    
    with open(srcjson, 'r') as src_file:
        data = json.load(src_file)
        data.update(extras)
    
    #
    # The cached data will be a local path.
    #
    scheme, _, cache_path, _, _, _ = urlparse(extras.get('cache', ''))
    if scheme == 'file':
        copy(cache_path, workdir)

    source_urls = data.get('cache')
    if not isinstance(source_urls, list):
        source_urls = [source_urls]

    task1 = URLDownloadTask(source)
    downloaded_path = task1.download(source_urls, workdir)
    _L.info("Downloaded to %s", downloaded_path)

    task2 = DecompressionTask.from_type_string(data.get('compression'))
    names = elaborate_filenames(data.get('conform', {}).get('file', None))
    decompressed_paths = task2.decompress(downloaded_path, workdir, names)
    _L.info("Decompressed to %d files", len(decompressed_paths))

    task3 = ExcerptDataTask()
    try:
        conform = data.get('conform', {})
        data_sample, geometry_type = task3.excerpt(decompressed_paths, workdir, conform)
        _L.info("Sampled %d records", len(data_sample))
    except Exception as e:
        _L.warning("Error doing excerpt; skipping", exc_info=True)
        data_sample = None
        geometry_type = None

    task4 = ConvertToCsvTask()
    try:
        csv_path, addr_count = task4.convert(data, decompressed_paths, workdir)
        _L.info("Converted to %s with %d addresses", csv_path, addr_count)
    except Exception as e:
        _L.warning("Error doing conform; skipping", exc_info=True)
        csv_path, addr_count = None, 0

    out_path = None
    if csv_path is not None and exists(csv_path):
        move(csv_path, join(destdir, 'out.csv'))
        out_path = realpath(join(destdir, 'out.csv'))

    rmtree(workdir)
    
    sharealike_flag = conform_sharealike(data.get('license'))
    attr_flag, attr_name = conform_attribution(data.get('license'), data.get('attribution'))

    return ConformResult(data.get('processed', None),
                         data_sample,
                         data.get('website'),
                         conform_license(data.get('license')),
                         geometry_type,
                         addr_count,
                         out_path,
                         datetime.now() - start,
                         sharealike_flag,
                         attr_flag,
                         attr_name)

def iterate_local_processed_files(runs, sort_on='datetime_tz'):
    ''' Yield a stream of local processed result files for a list of runs.
    
        Used in ci.collect and dotmap processes.
    '''
    if sort_on == 'source_path':
        reverse, key = False, lambda run: run.source_path
    else:
        reverse, key = True, lambda run: run.datetime_tz or date(1970, 1, 1)
    
    for run in sorted(runs, key=key, reverse=reverse):
        source_base, _ = splitext(relpath(run.source_path, 'sources'))
        processed_url = run.state and run.state.processed
        run_state = run.state
    
        if not processed_url:
            continue
        
        try:
            filename = download_processed_file(processed_url)
        except:
            _L.info('Retrying to download {}'.format(processed_url))
            try:
                filename = download_processed_file(processed_url)
            except:
                _L.info('Re-retrying to download {}'.format(processed_url))
                try:
                    filename = download_processed_file(processed_url)
                except:
                    _L.error('Failed to download {}'.format(processed_url))
                    continue
        
        yield LocalProcessedResult(source_base, filename, run_state, run.code_version)

        if filename and exists(filename):
            remove(filename)
    
def download_processed_file(url):
    ''' Download a URL to a local temporary file, return its path.
    
        Local file will have an appropriate timestamp and extension.
    '''
    _, ext = splitext(urlparse(url).path)
    handle, filename = mkstemp(prefix='processed-', suffix=ext)
    close(handle)
    
    response = get(url, stream=True, timeout=5)
    
    with open(filename, 'wb') as file:
        for chunk in response.iter_content(chunk_size=8192):
            file.write(chunk)
    
    last_modified = response.headers.get('Last-Modified')
    timestamp = timegm(parse(last_modified).utctimetuple())
    utime(filename, (timestamp, timestamp))
    
    return filename
