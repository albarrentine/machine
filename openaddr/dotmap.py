from __future__ import print_function, division
import logging; _L = logging.getLogger('openaddr.dotmap')

from .compat import standard_library

from sys import stderr
from zipfile import ZipFile
from os.path import splitext
from subprocess import Popen, PIPE
from argparse import ArgumentParser
from urllib.parse import urlparse, parse_qsl
from tempfile import mkstemp, gettempdir
from os import environ, close
import json

from .compat import csvIO, csvDictReader
from .ci import db_connect, db_cursor, setup_logger
from .ci.objects import read_latest_set, read_completed_runs_to_date
from . import iterate_local_processed_files

def connect_db(dsn):
    ''' Prepare old-style arguments to connect_db().
    '''
    p = urlparse(dsn)
    q = dict(parse_qsl(p.query))
    
    assert p.scheme == 'postgres'
    kwargs = dict(user=p.username, password=p.password, host=p.hostname)
    kwargs.update(dict(database=p.path.lstrip('/')))

    if 'sslmode' in q:
        kwargs.update(dict(sslmode=q['sslmode']))

    return db_connect(**kwargs)

parser = ArgumentParser(description='Make a dot map.')

parser.add_argument('-o', '--owner', default='openaddresses',
                    help='Github repository owner. Defaults to "openaddresses".')

parser.add_argument('-r', '--repository', default='openaddresses',
                    help='Github repository name. Defaults to "openaddresses".')

parser.add_argument('-d', '--database-url', default=environ.get('DATABASE_URL', None),
                    help='Optional connection string for database. Defaults to value of DATABASE_URL environment variable.')

def main():
    args = parser.parse_args()
    setup_logger(environ.get('AWS_SNS_ARN'))
    
    with connect_db(args.database_url) as conn:
        with db_cursor(conn) as db:
            set = read_latest_set(db, args.owner, args.repository)
            runs = read_completed_runs_to_date(db, set.id)
    
    handle, mbtiles_filename = mkstemp(prefix='oa-', suffix='.mbtiles')
    close(handle)
    
    cmd = 'tippecanoe', '-r', '2', '-l', 'openaddresses', \
          '-X', '-n', 'OpenAddresses YYYY-MM-DD', '-f', \
          '-t', gettempdir(), '-o', mbtiles_filename
    
    _L.info('Running tippcanoe: {}'.format(' '.join(cmd)))
    
    tippecanoe = Popen(cmd, stdin=PIPE, bufsize=1)
    zip_details = ((source, filename) for (source, filename, _)
                   in iterate_local_processed_files(runs))
    
    for feature in stream_all_features(zip_details):
        print(json.dumps(feature), file=tippecanoe.stdin)
    
    tippecanoe.stdin.close()
    tippecanoe.wait()

def stream_all_features(zip_details):
    ''' Generate a stream of all locations as GeoJSON features.
    '''
    for (source_base, zip_filename) in zip_details:
        _L.debug(u'Opening {} ({})'.format(zip_filename, source_base))

        zipfile = ZipFile(zip_filename, mode='r')
        for filename in zipfile.namelist():
            # Look for the one expected .csv file in the zip archive.
            _, ext = splitext(filename)
            if ext == '.csv':
                # Yield GeoJSON point objects with no properties.
                buffer = csvIO(zipfile.read(filename))
                for row in csvDictReader(buffer, encoding='utf8'):
                    try:
                        lon_lat = float(row['LON']), float(row['LAT'])
                        feature = {"type": "Feature", "properties": {}, 
                            "geometry": {"type": "Point", "coordinates": lon_lat}}
                    except ValueError:
                        pass
                    else:
                        yield feature

                # Move on to the next zip archive.
                zipfile.close()
                break

if __name__ == '__main__':
    exit(main())
