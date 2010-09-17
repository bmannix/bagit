#!/usr/bin/env python

"""
BagIt is a directory, filename convention for bundling an arbitrary set of
files with a manifest, checksums, and additional metadata. More about BagIt
can be found at:

    http://purl.org/net/bagit

bagit.py is a pure python drop in library and command line tool for creating,
and working with BagIt directories:

    import bagit
    bag = bagit.make_bag('example-directory', {'Contact-Name': 'Ed Summers'})
    print bag.entries

Basic usage is to give bag a directory to bag up:

    % bagit.py my_directory

You can bag multiple directories if you wish:

    % bagit.py directory1 directory2

Optionally you can pass metadata intended for the bag-info.txt:

    % bagit.py --source-organization "Library of Congress" directory

For more help see:

    % bagit.py --help
"""

import os
import hashlib
import logging
import optparse
import multiprocessing

from glob import glob
from datetime import date
from itertools import chain
from urllib import urlopen

# standard bag-info.txt metadata
_bag_info_headers = [
    'Source-Organization',
    'Organization-Address',
    'Contact-Name',
    'Contact-Phone',
    'Contact-Email',
    'External-Description',
    'External-Identifier',
    'Bag-Size',
    'Bag-Group-Identifier',
    'Bag-Count',
    'Internal-Sender-Identifier',
    'Internal-Sender-Description',
    # Bagging Date is autogenerated
    # Payload-Oxum is autogenerated
]

checksum_algos = ['md5', 'sha1']

def make_bag(bag_dir, bag_info=None, processes=1):
    """
    Convert a given directory into a bag. You can pass in arbitrary
    key/value pairs to put into the bag-info.txt metadata file as
    the bag_info dictionary.
    """
    logging.info("creating bag for directory %s" % bag_dir)

    if not os.path.isdir(bag_dir):
        logging.error("no such bag directory %s" % bag_dir)
        raise RuntimeError("no such bag directory %s" % bag_dir)

    old_dir = os.path.abspath(os.path.curdir)
    os.chdir(bag_dir)

    try:
        logging.info("creating data dir")
        os.mkdir('data')

        for f in os.listdir('.'):
            if f == 'data': continue
            new_f = os.path.join('data', f)
            logging.info("moving %s to %s" % (f, new_f))
            os.rename(f, new_f)

        logging.info("writing manifest-md5.txt")
        Oxum = _make_manifest('manifest-md5.txt', 'data', processes)

        logging.info("writing bagit.txt")
        txt = """BagIt-Version: 0.96\nTag-File-Character-Encoding: UTF-8\n"""
        open("bagit.txt", "w").write(txt)

        logging.info("writing bag-info.txt")
        bag_info_txt = open("bag-info.txt", "w")
        if bag_info == None:
            bag_info = {}
        bag_info['Bagging-Date'] = date.strftime(date.today(), "%Y-%m-%d")
        bag_info['Payload-Oxum'] = Oxum
        headers = bag_info.keys()
        headers.sort()
        for h in headers:
            bag_info_txt.write("%s: %s\n"  % (h, bag_info[h]))
        bag_info_txt.close()

    except Exception, e:
        logging.error(e)

    finally:
        os.chdir(old_dir)

    return Bag(bag_dir)



class BagError(Exception):
    pass

class BagValidationError(BagError):
    pass

class Bag(object):
    """A representation of a bag."""

    valid_files = ["bagit.txt", "fetch.txt"]
    valid_directories = ['data']

    def __init__(self, path=None):
        super(Bag, self).__init__()
        self.tags = {}
        self.entries = {}
        self.algs = []
        self.tag_file_name = None
        self.path = path
        if path:
            self._open()

    def __unicode__(self):
        return u'Bag(path="%s")' % self.path

    def _open(self):
        # Open the bagit.txt file, and load any tags from it, including
        # the required version and encoding.
        bagit_file_path = os.path.join(self.path, "bagit.txt")

        if not isfile(bagit_file_path):
            raise BagError("No bagit.txt found: %s" % bagit_file_path)

        self.tags = tags = _load_tag_file(bagit_file_path)

        try:
            self.version = tags["BagIt-Version"]
            self.encoding = tags["Tag-File-Character-Encoding"]
        except KeyError, e:
            raise BagError("Missing required tag in bagit.txt: %s" % e)

        if self.version == "0.95":
            self.tag_file_name = "package-info.txt"
        elif self.version == "0.96":
            self.tag_file_name = "bag-info.txt"
        else:
            raise BagError("Unsupported bag version: %s" % self.version)

        if not self.encoding.lower() == "utf-8":
            raise BagValidationError("Unsupported encoding: %s" % self.encoding)

        info_file_path = os.path.join(self.path, self.tag_file_name)
        if os.path.exists(info_file_path):
            self.info = _load_tag_file(info_file_path)

        self._load_manifests()

    def manifest_files(self):
        for filename in ["manifest-%s.txt" % a for a in checksum_algos]:
            f = os.path.join(self.path, filename)
            if isfile(f):
                yield f

    def tagmanifest_files(self):
        for filename in ["tagmanifest-%s.txt" % a for a in checksum_algos]:
            f = os.path.join(self.path, filename)
            if isfile(f):
                yield f

    def compare_manifests_with_fs(self):
        files_on_fs = set(self.payload_files())
        files_in_manifest = set(self.entries.keys())

        return (list(files_in_manifest - files_on_fs),
             list(files_on_fs - files_in_manifest))

    def compare_fetch_with_fs(self):
        """Compares the fetch entries with the files actually
           in the payload, and returns a list of all the files
           that still need to be fetched.
        """

        files_on_fs = set(self.payload_files())
        files_in_fetch = set(self.files_to_be_fetched())

        return list(files_in_fetch - files_on_fs)

    def payload_files(self):
        payload_dir = os.path.join(self.path, "data")

        for dirpath, dirnames, filenames in os.walk(payload_dir):
            for f in filenames:
                # Jump through some hoops here to make the payload files come out
                # looking like data/dir/file, rather than having the entire path.
                rel_path = os.path.join(dirpath, os.path.normpath(f.replace('\\', '/')))
                rel_path = rel_path.replace(self.path + os.path.sep, "", 1)
                yield rel_path

    def fetch_entries(self):
        fetch_file_path = os.path.join(self.path, "fetch.txt")

        if isfile(fetch_file_path):
            fetch_file = urlopen(fetch_file_path)

            try:
                for line in fetch_file:
                    parts = line.strip().split(None, 2)
                    yield (parts[0], parts[1], parts[2])
            finally:
                fetch_file.close()

    def files_to_be_fetched(self):
        for url, size, path in self.fetch_entries():
            yield path

    def urls_to_be_fetched(self):
        for url, size, path in self.fetch_entries():
            yield url

    def has_oxum(self):
        return self.tags.has_key('Payload-Oxum')

    def validate(self):
        """Checks the structure and contents are valid
        """
        self._validate_structure()
        self._validate_contents()
        return True

    def _load_manifests(self):
        for manifest_file in self.manifest_files():
            alg = os.path.basename(manifest_file).replace("manifest-", "").replace(".txt", "")
            self.algs.append(alg)

            manifest_file = urlopen(manifest_file)

            try:
                for line in manifest_file:
                    print line
                    line = line.strip()

                    # Ignore blank lines and comments.
                    if line == "" or line.startswith("#"): continue

                    entry = line.split(None, 1)

                    # Format is FILENAME *CHECKSUM
                    if len(entry) != 2:
                        logging.error("%s: Invalid %s manifest entry: %s", self, alg, line)
                        continue

                    entry_hash = entry[0]
                    entry_path = os.path.normpath(entry[1].lstrip("*"))

                    if self.entries.has_key(entry_path):
                        if self.entries[entry_path].has_key(alg):
                            logging.warning("%s: Duplicate %s manifest entry: %s", self, alg, entry_path)

                        self.entries[entry_path][alg] = entry_hash
                    else:
                        self.entries[entry_path] = {}
                        self.entries[entry_path][alg] = entry_hash
            finally:
                manifest_file.close()

    def _validate_structure(self):
        """Checks the structure of the bag, determining if it conforms to the
           BagIt spec. Returns true on success, otherwise it will raise
           a BagValidationError exception.
        """
        self._validate_structure_payload_directory()
        self._validate_structure_tag_files()

    def _validate_structure_payload_directory(self):
        data_dir_path = os.path.join(self.path, "data")

        if not isdir(data_dir_path):
            raise BagValidationError("Missing data directory.")

    def _validate_structure_tag_files(self):
        # Files allowed in all versions are:
        #  - tagmanifest-<alg>.txt
        #  - manifest-<alt>.txt
        #  - bagit.txt
        #  - fetch.txt
        valid_files = list(self.valid_files)

        # The manifest files and tagmanifest files will start with {self.path}/
        # So strip that off.
        for f in chain(self.manifest_files(), self.tagmanifest_files()):
            valid_files.append(f[len(self.path) + 1:])

        for name in os.listdir(self.path):
            fullname = os.path.join(self.path, name)

            if isdir(fullname):
                if not name in self.valid_directories:
                    raise BagValidationError("Extra directory found: %s" % name)
            elif isfile(fullname):
                if not name in valid_files:
                    is_valid = self._validate_structure_is_valid_tag_file_name(name)
                    if not is_valid:
                        raise BagValidationError("Extra tag file found: %s" % name)
            else:
                # Something that's  neither a dir or a file. WTF?
                raise BagValidationError("Unknown item in bag: %s" % name)

    def _validate_structure_is_valid_tag_file_name(self, file_name):
        return file_name == self.tag_file_name

    def _validate_contents(self):
        """
        Validate the contents of this bag, which can be a very time-consuming
        operation
        """
        self._validate_oxum()    # Fast
        self._validate_entries() # *SLOW*

    def _validate_oxum(self):
        oxum = self.tags.get('Payload-Oxum')
        if oxum == None: return

        byte_count, file_count = oxum.split('.', 1)

        if not byte_count.isdigit() or not file_count.isdigit():
            raise BagError("Invalid oxum: %s" % oxum)

        byte_count = long(byte_count)
        file_count = long(file_count)
        total_bytes = 0
        total_files = 0

        for payload_file in self.payload_files():
            payload_file = os.path.join(self.path, payload_file)
            total_bytes += os.stat(payload_file).st_size
            total_files += 1

        if file_count != total_files or byte_count != total_bytes:
            raise BagError("Oxum error.  Found %s files and %s bytes on disk; expected %s files and %s bytes." % (total_files, total_bytes, file_count, byte_count))

    def _validate_entries(self):
        """
        Verify that the actual file contents match the recorded hashes stored in the manifest files
        """
        errors = list()

        # To avoid the overhead of reading the file more than once or loading
        # potentially massive files into memory we'll create a dictionary of
        # hash objects so we can open a file, read a block and pass it to
        # multiple hash objects

        hashers = {}
        for alg in self.algs:
            try:
                hashers[alg] = hashlib.new(alg)
            except KeyError:
                logging.warning("Unable to validate file contents using unknown %s hash algorithm", alg)

        if not hashers:
            raise RuntimeError("%s: Unable to validate bag contents: none of the hash algorithms in %s are supported!" % (self, self.algs))

        for rel_path, hashes in self.entries.items():
            full_path = os.path.join(self.path, rel_path)

            # Create a clone of the default empty hash objects:
            f_hashers = dict(
                (alg, h.copy()) for alg, h in hashers.items() if alg in hashes
            )

            try:
                f_hashes = self._calculate_file_hashes(full_path, hashers)
            except BagValidationError, e:
                raise e
            # Any unhandled exceptions are probably fatal
            except:
                logging.exception("unable to calculate file hashes for %s: %s", self, full_path)
                raise

            for alg, stored_hash in f_hashes.items():
                computed_hash = f_hashes[alg]
                if stored_hash != computed_hash:
                    logging.warning("%s: stored hash %s doesn't match calculated hash %s", full_path, stored_hash, computed_hash)
                    errors.append("%s (%s)" % (full_path, alg))

        if errors:
            raise BagValidationError("%s: %d files failed checksum validation: %s" % (self, len(errors), errors))

    def _calculate_file_hashes(self, full_path, f_hashers):
        """
        Returns a dictionary of (algorithm, hexdigest) values for the provided
        filename
        """
        if not os.path.exists(full_path):
            raise BagValidationError("%s does not exist" % full_path)

        f = urlopen(full_path)

        f_size = os.stat(full_path).st_size

        while True:
            block = f.read(1048576)
            if not block:
                break
            [ i.update(block) for i in f_hashers.values() ]
        f.close()

        return dict(
            (alg, h.hexdigest()) for alg, h in f_hashers.items()
        )

def _load_tag_file(tag_file_name):
    tag_file = urlopen(tag_file_name)

    try:
        return dict(_parse_tags(tag_file))
    finally:
        tag_file.close()

def _parse_tags(file):
    """Parses a tag file, according to RFC 2822.  This
       includes line folding, permitting extra-long
       field values.

       See http://www.faqs.org/rfcs/rfc2822.html for
       more information.
    """

    tag_name = None
    tag_value = None

    # Line folding is handled by yielding values
    # only after we encounter the start of a new
    # tag, or if we pass the EOF.
    for line in file:
        # Skip over any empty or blank lines.
        if len(line) == 0 or line.isspace():
            continue

        if line[0].isspace(): # folded line
            tag_value += line.strip()
        else:
            # Starting a new tag; yield the last one.
            if tag_name:
                yield (tag_name, tag_value)

            parts = line.strip().split(':', 1)
            tag_name = parts[0].strip()
            tag_value = parts[1].strip()

    # Passed the EOF.  All done after this.
    if tag_name:
        yield (tag_name, tag_value)


def _make_manifest(manifest_file, data_dir, processes):
    logging.info('writing manifest with %s processes' % processes)
    pool = multiprocessing.Pool(processes=processes)
    manifest = open(manifest_file, 'w')
    num_files = 0
    total_bytes = 0
    for digest, filename, bytes in pool.map(_manifest_line, _walk(data_dir)):
        num_files += 1
        total_bytes += bytes
        manifest.write("%s  %s\n" % (digest, filename))
    manifest.close()
    return "%s.%s" % (total_bytes, num_files)

def _walk(data_dir):
    for dirpath, dirnames, filenames in os.walk(data_dir):
        for fn in filenames:
            yield os.path.join(dirpath, fn)

def _manifest_line(filename):
    fh = urlopen(filename)
    m = hashlib.md5()
    total_bytes = 0
    while True:
        bytes = fh.read(16384)
        total_bytes += len(bytes)
        if not bytes: break
        m.update(bytes)
    fh.close()
    return (m.hexdigest(), filename, total_bytes)


# following code is used for command line program

class BagOptionParser(optparse.OptionParser):
    def __init__(self, *args, **opts):
        self.bag_info = {}
        optparse.OptionParser.__init__(self, *args, **opts)

def _bag_info_store(option, opt, value, parser):
    opt = opt.lstrip('--')
    opt_caps = '-'.join([o.capitalize() for o in opt.split('-')])
    parser.bag_info[opt_caps] = value

def _make_opt_parser():
    parser = BagOptionParser(usage='usage: %prog [options] dir1 dir2 ...')
    parser.add_option('--processes', action='store', type="int",
                      dest='processes', default=1,
                      help='parallelize checksums generation')
    parser.add_option('--log', action='store', dest='log')
    parser.add_option('--quiet', action='store_true', dest='quiet')

    for header in _bag_info_headers:
        parser.add_option('--%s' % header.lower(), type="string",
                          action='callback', callback=_bag_info_store)
    return parser

def _configure_logging(opts):
    log_format="%(asctime)s - %(levelname)s - %(message)s"
    if opts.quiet:
        level = logging.ERROR
    else:
        level = logging.INFO
    if opts.log:
        logging.basicConfig(filename=opts.log, level=level, format=log_format)
    else:
        logging.basicConfig(level=level, format=log_format)

def isfile(path):
    if path.startswith('http://'):
        return urlopen(path).getcode() == 200
    return os.path.isfile(path)

def isdir(path):
    if path.startswith('http://'):
        return urlopen(path).getcode() == 200
    return os.path.isdir(path)

if __name__ == '__main__':
    opt_parser = _make_opt_parser()
    opts, args = opt_parser.parse_args()
    _configure_logging(opts)
    for bag_dir in args:
        make_bag(bag_dir, bag_info=opt_parser.bag_info, 
                processes=opts.processes)