#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys

import oscar_payonline

try:
    from setuptools import setup
except ImportError:
    from distutils.core import setup

version = oscar_payonline.__version__

if sys.argv[-1] == 'publish':
    os.system('python setup.py sdist upload')
    print("You probably want to also tag the version now:")
    print("  git tag -a %s -m 'version %s'" % (version, version))
    print("  git push --tags")
    sys.exit()

readme = open('README.rst').read()
history = open('HISTORY.rst').read().replace('.. :changelog:', '')

setup(
    name='django-oscar-payonline',
    version=version,
    description="""Payonline.ru Payment Extension for Oscar E-Commerce""",
    long_description=readme + '\n\n' + history,
    author='Oleg Rybkin aka Fish',
    author_email='okfish@yandex.ru',
    url='https://github.com/okfish/django-oscar-payonline',
    packages=[
        'oscar_payonline',
    ],
    include_package_data=True,
    install_requires=[
    ],
    license="BSD",
    zip_safe=False,
    keywords='oscar_payonline',
    classifiers=[
        'Development Status :: 2 - Pre-Alpha',
        'Framework :: Django',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: BSD License',
        'Natural Language :: English',
        'Programming Language :: Python :: 2',
        'Programming Language :: Python :: 2.7',
    ],
)
