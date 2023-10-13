#!/usr/bin/env python

from io import open
from setuptools import setup, find_packages

NAME = 'django-redis-aiogram'
VERSION = '1.0.4'


def read_md(file_path):
    with open(file_path) as fp:
        return fp.read()


setup(
    name=NAME,
    version=VERSION,
    install_requires=(
        'django>=4',
        'redis>=4.6',
        'aiogram>=3.1.1'
    ),
    author='Oleksii Kolosiuk',
    author_email='kolosyuk1@gmail.com',
    license='MIT',
    url='https://github.com/CorneiZeR/django-redis-aiogram',
    keywords='django redis aiogram docker',
    description='Running aiogram in neighbor container, sending messages to telegram via redis',
    long_description=read_md('README.md'),
    long_description_content_type='text/markdown',
    packages=find_packages(),
    python_requires='>=3.8',
    include_package_data=True,
    zip_safe=False,
    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: MIT License',
        'Topic :: Software Development :: Libraries :: Python Modules',
        'Programming Language :: Python',
        "Programming Language :: Python :: 3",
        'Framework :: Django',
        'Framework :: Django :: 4',
    ]
)