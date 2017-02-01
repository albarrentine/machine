from __future__ import absolute_import, division, print_function
import logging; _L = logging.getLogger('openaddr.process_one')

from urllib.parse import urlparse
from os.path import join, basename, dirname, exists, splitext, relpath
from shutil import copy, move, rmtree
from argparse import ArgumentParser
from os import mkdir, rmdir, close, chmod
from _thread import get_ident
import tempfile, json, csv, sys

from . import cache, conform, preview, slippymap, CacheResult, ConformResult, __version__
from .cache import DownloadError

from esridump.errors import EsriDownloadError

class SourceSaysSkip(RuntimeError): pass

def boolstr(value):
    '''
    '''
    if value is True:
        return 'true'
    
    if value is False:
        return 'false'
    
    if value is None:
        return ''
    
    raise ValueError(repr(value))

def process(source, destination, do_preview, mapzen_key=None, extras=dict()):
    ''' Process a single source and destination, return path to JSON state file.
    
        Creates a new directory and files under destination.
    '''
    temp_dir = tempfile.mkdtemp(prefix='process_one-', dir=destination)
    temp_src = join(temp_dir, basename(source))
    copy(source, temp_src)
    
    log_handler = get_log_handler(temp_dir)
    logging.getLogger('openaddr').addHandler(log_handler)
    
    cache_result, conform_result = CacheResult.empty(), ConformResult.empty()
    preview_path, slippymap_path, skipped_source = None, None, False

    try:
        with open(temp_src) as file:
            if json.load(file).get('skip', None):
                raise SourceSaysSkip()
    
        # Cache source data.
        try:
            cache_result = cache(temp_src, temp_dir, extras)
        except EsriDownloadError as e:
            _L.warning('Could not download ESRI source data: {}'.format(e))
            raise
        except DownloadError as e:
            _L.warning('Could not download source data')
            raise
    
        if not cache_result.cache:
            _L.warning('Nothing cached')
        else:
            _L.info(u'Cached data in {}'.format(cache_result.cache))

            # Conform cached source data.
            conform_result = conform(temp_src, temp_dir, cache_result.todict())
    
            if not conform_result.path:
                _L.warning('Nothing processed')
            else:
                _L.info('Processed data in {}'.format(conform_result.path))
                
                if do_preview and mapzen_key:
                    preview_path = render_preview(conform_result.path, temp_dir, mapzen_key)
                
                if do_preview:
                    slippymap_path = render_slippymap(conform_result.path, temp_dir)

                if not preview_path:
                    _L.warning('Nothing previewed')
                else:
                    _L.info('Preview image in {}'.format(preview_path))
    
    except SourceSaysSkip as e:
        _L.info('Source says to skip in process_one.process()')
        skipped_source = True

    except Exception:
        _L.warning('Error in process_one.process()', exc_info=True)
    
    finally:
        # Make sure this gets done no matter what
        logging.getLogger('openaddr').removeHandler(log_handler)

    # Write output
    state_path = write_state(source, skipped_source, destination, log_handler,
        cache_result, conform_result, preview_path, slippymap_path, temp_dir)

    log_handler.close()
    rmtree(temp_dir)
    return state_path

def render_preview(csv_filename, temp_dir, mapzen_key):
    '''
    '''
    png_filename = join(temp_dir, 'preview.png')
    preview.render(csv_filename, png_filename, 668, 2, mapzen_key)

    return png_filename

def render_slippymap(csv_filename, temp_dir):
    '''
    '''
    try:
        mbtiles_filename = join(temp_dir, 'slippymap.mbtiles')
        slippymap.generate(csv_filename, mbtiles_filename)
    except Exception as e:
        _L.error('%s in render_slippymap: %s', type(e), e)
        return None
    else:
        return mbtiles_filename

class LogFilter:
    ''' Logging filter object to match only record in the current thread.
    '''
    def __init__(self):
        # Seems to work as unique ID with multiprocessing.Process() as well as threading.Thread()
        self.thread_id = get_ident()
    
    def filter(self, record):
        return record.thread == self.thread_id

def get_log_handler(directory):
    ''' Create a new file handler for the current thread and return it.
    '''
    handle, filename = tempfile.mkstemp(dir=directory, suffix='.log')
    close(handle)
    chmod(filename, 0o644)
    
    handler = logging.FileHandler(filename)
    handler.setFormatter(logging.Formatter(u'%(asctime)s %(levelname)08s: %(message)s'))
    handler.setLevel(logging.DEBUG)
    handler.addFilter(LogFilter())
    
    return handler

def find_source_problem(log_contents, source):
    '''
    '''
    if 'WARNING: Source is missing a conform object' in log_contents:
        return 'Source is missing a conform object'
    
    if 'WARNING: Unknown source conform type' in log_contents:
        return 'Unknown source conform type'
    
    if 'WARNING: Could not download source data' in log_contents:
        return 'Could not download source data'
    
    if 'WARNING: Error doing conform; skipping' in log_contents:
        return 'Could not conform source data'
    
    if 'WARNING: Could not download ESRI source data: Could not retrieve layer metadata: Token Required' in log_contents:
        return 'Missing required ESRI token'
    
    if 'coverage' in source:
        coverage = source.get('coverage')
        if 'US Census' in coverage or 'ISO 3166' in coverage or 'geometry' in coverage:
            pass
        else:
            return 'Missing or incomplete coverage'
    else:
        return 'Missing or incomplete coverage'
    
    return None

def write_state(source, skipped, destination, log_handler, cache_result,
                conform_result, preview_path, slippymap_path, temp_dir):
    '''
    '''
    source_id, _ = splitext(basename(source))
    statedir = join(destination, source_id)
    
    if not exists(statedir):
        mkdir(statedir)
    
    if cache_result.cache:
        scheme, _, cache_path1, _, _, _ = urlparse(cache_result.cache)
        if scheme in ('file', ''):
            cache_path2 = join(statedir, 'cache{1}'.format(*splitext(cache_path1)))
            copy(cache_path1, cache_path2)
            state_cache = relpath(cache_path2, statedir)
        else:
            state_cache = cache_result.cache
    else:
        state_cache = None

    if conform_result.path:
        _, _, processed_path1, _, _, _ = urlparse(conform_result.path)
        processed_path2 = join(statedir, 'out{1}'.format(*splitext(processed_path1)))
        copy(processed_path1, processed_path2)

    # Write the sample data to a sample.json file
    if conform_result.sample:
        sample_path = join(statedir, 'sample.json')
        with open(sample_path, 'w') as sample_file:
            json.dump(conform_result.sample, sample_file, indent=2)
    
    if preview_path:
        preview_path2 = join(statedir, 'preview.png')
        copy(preview_path, preview_path2)
    
    if slippymap_path:
        slippymap_path2 = join(statedir, 'slippymap.mbtiles')
        copy(slippymap_path, slippymap_path2)
    
    log_handler.flush()
    output_path = join(statedir, 'output.txt')
    copy(log_handler.stream.name, output_path)

    if skipped:
        source_problem = 'Source says to skip'
    else:
        with open(output_path) as file:
            log_content = file.read()
        if exists(source):
            with open(source) as file:
                source_data = json.load(file)
        else:
            source_data = {}
        source_problem = find_source_problem(log_content, source_data)

    state = [
        ('source', basename(source)),
        ('skipped', bool(skipped)),
        ('cache', state_cache),
        ('sample', conform_result.sample and relpath(sample_path, statedir)),
        ('website', conform_result.website),
        ('license', conform_result.license),
        ('geometry type', conform_result.geometry_type),
        ('address count', conform_result.address_count),
        ('version', cache_result.version),
        ('fingerprint', cache_result.fingerprint),
        ('cache time', cache_result.elapsed and str(cache_result.elapsed)),
        ('processed', conform_result.path and relpath(processed_path2, statedir)),
        ('process time', conform_result.elapsed and str(conform_result.elapsed)),
        ('output', relpath(output_path, statedir)),
        ('preview', preview_path and relpath(preview_path2, statedir)),
        ('slippymap', slippymap_path and relpath(slippymap_path2, statedir)),
        ('attribution required', boolstr(conform_result.attribution_flag)),
        ('attribution name', conform_result.attribution_name),
        ('share-alike', boolstr(conform_result.sharealike_flag)),
        ('source problem', source_problem),
        ('code version', __version__),
        ]
               
    with open(join(statedir, 'index.txt'), 'w', encoding='utf8') as file:
        out = csv.writer(file, dialect='excel-tab')
        for row in zip(*state):
            out.writerow(row)
    
    with open(join(statedir, 'index.json'), 'w') as file:
        json.dump(list(zip(*state)), file, indent=2)
               
        _L.info(u'Wrote to state: {}'.format(file.name))
        return file.name

parser = ArgumentParser(description='Run one source file locally, prints output path.')

parser.add_argument('source', help='Required source file name.')
parser.add_argument('destination', help='Required output directory name.')

parser.add_argument('--render-preview', help='Render a map preview',
                    action='store_const', dest='render_preview',
                    const=True, default=False)

parser.add_argument('--skip-preview', help="Don't render a map preview",
                    action='store_const', dest='render_preview',
                    const=False, default=False)

parser.add_argument('--mapzen-key', dest='mapzen_key',
                    help='Mapzen API Key. See: https://mapzen.com/documentation/overview/')

parser.add_argument('-l', '--logfile', help='Optional log file name.')

parser.add_argument('-v', '--verbose', help='Turn on verbose logging',
                    action='store_const', dest='loglevel',
                    const=logging.DEBUG, default=logging.INFO)

parser.add_argument('-q', '--quiet', help='Turn off most logging',
                    action='store_const', dest='loglevel',
                    const=logging.WARNING, default=logging.INFO)

def main():
    '''
    '''
    from .jobs import setup_logger

    args = parser.parse_args()
    setup_logger(logfile=args.logfile, log_level=args.loglevel)
    
    # Allow CSV files with very long fields
    csv.field_size_limit(sys.maxsize)
    
    try:
        file_path = process(args.source, args.destination, args.render_preview, mapzen_key=args.mapzen_key)
    except Exception as e:
        _L.error(e, exc_info=True)
        return 1
    else:
        print(file_path)
        return 0

if __name__ == '__main__':
    exit(main())
