from setuptools import setup

description = \
"""
This package can be used to create BagIt style packages of
digital content for safe transmission and digital preservation.
See: http://en.wikipedia.org/wiki/BagIt for more details.
"""

setup(
    name = 'bagit',
    version = '0.9.7',
    url = 'http://github.com/edsu/bagit',
    author = 'Ed Summers',
    author_email = 'ehs@pobox.com',
    py_modules = ['bagit',],
    scripts = ['bagit.py'],
    description = description,
    platforms = ['POSIX'],
    test_suite = 'test',
    classifiers = [
        'License :: Public Domain',
        'Intended Audience :: Developers',
        'Topic :: Communications :: File Sharing',
        'Topic :: Software Development :: Libraries :: Python Modules',
        'Topic :: System :: Filesystems',
    ],
)
