"""MongoDB client and database accessors with production pool settings."""

from __future__ import annotations

from functools import lru_cache

import gridfs
from pymongo import MongoClient
from pymongo.database import Database

from app.settings import get_settings


@lru_cache
def get_mongo_client() -> MongoClient:
    settings = get_settings()
    return MongoClient(
        settings.mongodb_uri,
        maxPoolSize=settings.mongodb_max_pool_size,
        minPoolSize=settings.mongodb_min_pool_size,
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=10000,
        socketTimeoutMS=30000,
        retryWrites=True,
        appname="screening-assist",
    )


def get_db() -> Database:
    settings = get_settings()
    return get_mongo_client()[settings.mongodb_db]


@lru_cache
def get_gridfs() -> gridfs.GridFS:
    return gridfs.GridFS(get_db())
