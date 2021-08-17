import abc
import asyncio
import copy
import os
from collections import UserDict
from copy import deepcopy
from functools import partial
from typing import NamedTuple, Optional

from biothings.hub import INDEXER_CATEGORY, INDEXMANAGER_CATEGORY
from biothings.hub.databuild.backend import (create_backend,
                                             merge_src_build_metadata)
from biothings.utils.common import (get_class_from_classpath,
                                    get_random_string, iter_n, traverse)
from biothings.utils.es import ESIndexer
from biothings.utils.hub_db import get_src_build
from biothings.utils.loggers import get_logger
from biothings.utils.manager import BaseManager
from biothings.utils.mongo import id_feeder
from elasticsearch import AsyncElasticsearch
from pymongo.mongo_client import MongoClient

from .indexer_payload import *
from .indexer_registrar import *
from .indexer_schedule import Schedule
from .indexer_task import dispatch

# Summary
# -------
# IndexManager: a hub feature, providing top level commands and config environments(env).
# Indexer/ColdHotIndexer: the "index" command, handles jobs, db state and errors.
# .indexer_task.IndexingTask: index a set of ids, running independent of the hub.

# TODO
# Clarify returned result
# Distinguish creates/updates/deletes

# TODO
# Multi-layer logging


class IndexerException(Exception):
    ...

class ProcessInfo():

    def __init__(self, indexer, concurrency):
        self.indexer = indexer
        self.concurrency = concurrency

    def get_predicates(self):
        def limit_indexer_concurrency(job_manager):
            def by_indexer_environment(job):
                return all((
                    job["category"] == INDEXER_CATEGORY,
                    job["source"] == self.indexer.env_name
                ))
            return len(list(filter(
                by_indexer_environment,
                job_manager.jobs.values()
            ))) < self.concurrency
        return [limit_indexer_concurrency]

    def get_pinfo(self, step="", description=""):
        """
        Return dict containing information about the current process
        (used to report in the hub)
        """
        pinfo = {
            "__predicates__": self.get_predicates(),
            "category": INDEXER_CATEGORY,
            "source": self.indexer.env_name,
            "description": description,
            "step": step
        }
        return pinfo

class _BuildBackend(NamedTuple):  # mongo
    args: dict = {}
    dbs: Optional[str] = None
    col: Optional[str] = None

class _BuildDoc(UserDict):
    """ Represent A Build Under "src_build" Collection.

    Example:
    {
        "_id":"mynews_202105261855_5ffxvchx",
        "target_backend": "mongo",
        "target_name": "mynews_202105261855_5ffxvchx",
        "backend_url": "mynews_202105261855_5ffxvchx",
        "build_config": {
            "_id": "mynews",
            "name": "mynews",
            "doc_type": "news",
            ...
            "cold_collection": "mynews_202012280220_vsdevjdk"
        },
        "mapping": {
            "author": {"type": "text" },
            "title": {"type": "text" },
            "description": {"type": "text" },
            ...
        },
        "_meta": {
            "biothing_type": "news",
            "build_version": "202105261855",
            "build_date": "2021-05-26T18:55:00.054622+00:00",
            ...
        },
        ...
    }
    """
    @property
    def target_name(self):
        return self.get("target_name", self.get("_id"))

    @property
    def build_config(self):
        return self.get("build_config", {})

    def enrich_mappings(self, mappings):
        mappings["__hub_doc_type"] = self.build_config.get("doc_type")
        mappings["properties"].update(self.get("mapping", {}))
        mappings["_meta"] = self.get("_meta", {})

    def enrich_settings(self, settings):
        settings["number_of_shards"] = self.build_config.get("num_shards", 1)
        settings["number_of_replicas"] = self.build_config.get("num_replicas", 0)

    def parse_backend(self):
        # Support Sebastian's hub style backend URI
        # #biothings.hub.databuild.backend.create_backend
        backend = self.get("target_backend")
        backend_url = self.get("backend_url")

        if backend is None:
            return _BuildBackend()

        elif backend == "mongo":
            from biothings.hub.databuild import backend

            db = backend.mongo.get_target_db()
            if backend_url in db.list_collection_names():
                return _BuildBackend(
                    dict(zip(
                        ("host", "port"),
                        db.client.address
                    )), db.name, backend_url)

        elif backend == "link":
            from biothings.hub.databuild import backend

            if backend_url[0] == "src":
                db = backend.mongo.get_src_db()
            else:  # backend_url[0] == "target"
                db = backend.mongo.get_target_db()

            if backend_url[1] in db.list_collection_names():
                return _BuildBackend(
                    dict(zip(
                        ("host", "port"),
                        db.client.address
                    )), db.name, backend_url[1])

        raise ValueError(backend, backend_url)

    def extract_coldbuild(self):
        cold_target = self.build_config["cold_collection"]
        cold_build_doc = get_src_build().find_one({'_id': cold_target})

        cold_build_doc["mapping"].update(self["mapping"])  # combine mapping
        merge_src_build_metadata([cold_build_doc, self])  # combine _meta

        return _BuildDoc(cold_build_doc)


class Step(abc.ABC):
    name: property(abc.abstractmethod(lambda _: ...))
    state: property(abc.abstractmethod(lambda _: ...))
    method: property(abc.abstractmethod(lambda _: ...))
    catelog = dict()

    def __init__(self, indexer):
        self.indexer = indexer
        self.state = self.state(indexer, get_src_build())

    @classmethod
    def __init_subclass__(cls):
        cls.catelog[cls.name] = cls

    @classmethod
    def dispatch(cls, name):
        return cls.catelog[name]

    @asyncio.coroutine
    def execute(self, *args, **kwargs):
        coro = getattr(self.indexer, self.method)
        coro = coro(*args, **kwargs)
        return (yield from coro)

    def __str__(self):
        return (
            f"<Step"
            f" name='{self.name}'"
            f" indexer={self.indexer}"
            f">"
        )

class PreIndexStep(Step):
    name = "pre"
    state = PreIndexJSR
    method = "pre_index"

class MainIndexStep(Step):
    name = "index"
    state = MainIndexJSR
    method = "do_index"

class PostIndexStep(Step):
    name = "post"
    state = PostIndexJSR
    method = "post_index"

class _IndexerResult(UserDict):  # TODO Make this common

    def __str__(self):
        return f"{type(self).__name__}({str(self.data)})"

class IndexerIndexResult(_IndexerResult):
    ...

class IndexerStepResult(_IndexerResult):
    ...

class Indexer():
    """
    MongoDB -> Elasticsearch Indexer.
    """

    def __init__(self, build_doc, indexer_env, index_name):

        # build_doc primarily describes the source collection.
        # indexer_env primarily describes the destination index.

        _build_doc = _BuildDoc(build_doc)
        _build_backend = _build_doc.parse_backend()

        # ----------source----------

        self.mongo_client_args = _build_backend.args
        self.mongo_database_name = _build_backend.dbs
        self.mongo_collection_name = _build_backend.col

        # -----------dest-----------

        self.es_client_args = indexer_env.get("args", {})
        self.es_blkidx_args = indexer_env.get("bulk", {})
        self.es_index_name = index_name or _build_doc.target_name
        self.es_index_settings = IndexSettings(deepcopy(DEFAULT_INDEX_SETTINGS))
        self.es_index_mappings = IndexMappings(deepcopy(DEFAULT_INDEX_MAPPINGS))

        _build_doc.enrich_settings(self.es_index_settings)
        _build_doc.enrich_mappings(self.es_index_mappings)

        # ----------logging----------

        self.env_name = indexer_env.get("name")
        self.conf_name = _build_doc.build_config.get("name")
        self.target_name = _build_doc.target_name  # name of the build
        self.logger, self.logfile = get_logger('index_%s' % self.es_index_name)

        self.pinfo = ProcessInfo(self, indexer_env.get("concurrency", 3))

    def __str__(self):
        showx = self.mongo_collection_name != self.es_index_name
        lines = [
            f"<{type(self).__name__}",
            f" source='{self.mongo_collection_name}'" if showx else "",
            f" dest='{self.es_index_name}'"
            f">"
        ]
        return "".join(lines)

    # --------------
    #  Entry Point
    # --------------

    @asyncio.coroutine
    def index(self, job_manager, **kwargs):
        """
        Build an Elasticsearch index (self.es_index_name)
        with data from MongoDB collection (self.mongo_collection_name).

        "ids" can be passed to selectively index documents.

        "mode" can have the following values:
            - 'purge': will delete an index if it exists.
            - 'resume': will use an existing index and add missing documents.
            - 'merge': will merge data to an existing index.
            - 'index' (default): will create a new index.
        """

        steps = kwargs.pop("steps", ("pre", "index", "post"))
        batch_size = kwargs.setdefault("batch_size", 10000)
        mode = kwargs.setdefault("mode", "index")
        ids = kwargs.setdefault("ids", None)

        if isinstance(steps, str):
            steps = [steps]

        assert job_manager
        assert all(isinstance(_id, str) for _id in ids) if ids else True
        assert 50 <= batch_size <= 10000, '"batch_size" out-of-range'
        assert isinstance(steps, (list, tuple)), 'bad argument "steps"'
        assert isinstance(mode, str), 'bad argument "mode"'

        # the batch size here controls only the task partitioning
        # it does not affect how the elasticsearch python client
        # makes batch requests. a number larger than 10000 may exceed
        # es result window size and doc_feeder maximum fetch size.
        # a number smaller than 50 is too small that the documents
        # can be sent to elasticsearch within one request, making it
        # inefficient, amplifying the scheduling overhead.

        x = IndexerIndexResult()
        for step in steps:
            step = Step.dispatch(step)(self)
            self.logger.info(step)
            step.state.started()
            try:
                dx = yield from step.execute(job_manager, **kwargs)
                dx = IndexerStepResult(dx)
            except Exception as exc:
                _exc = str(exc)[:500]
                self.logger.exception(_exc)
                step.state.failed(_exc)
                raise exc
            else:
                merge(x.data, dx.data)
                self.logger.info(dx)
                self.logger.info(x)
                step.state.succeed({
                    self.es_index_name: x.data
                })

        return x

    # ---------
    #   Steps
    # ---------

    @asyncio.coroutine
    def pre_index(self, *args, mode, **kwargs):

        client = AsyncElasticsearch(**self.es_client_args)
        try:
            if mode in ("index", None):

                # index MUST NOT exist
                # ----------------------

                if (yield from client.indices.exists(self.es_index_name)):
                    msg = ("Index '%s' already exists, (use mode='purge' to "
                           "auto-delete it or mode='resume' to add more documents)")
                    raise IndexerException(msg % self.es_index_name)

            elif mode in ("resume", "merge"):

                # index MUST exist
                # ------------------

                if not (yield from client.indices.exists(self.es_index_name)):
                    raise IndexerException("'%s' does not exist." % self.es_index_name)
                self.logger.info(("Exists", self.es_index_name))
                return  # skip index creation

            elif mode == "purge":

                # index MAY exist
                # -----------------

                response = yield from client.indices.delete(self.es_index_name, ignore_unavailable=True)
                self.logger.info(("Deleted", self.es_index_name, response))

            else:
                raise ValueError("Invalid mode: %s" % mode)

            response = yield from client.indices.create(self.es_index_name, body={
                "settings": (yield from self.es_index_settings.finalize(client)),
                "mappings": (yield from self.es_index_mappings.finalize(client))
            })
            self.logger.info(("Created", self.es_index_name, response))

        finally:
            yield from client.close()

    @asyncio.coroutine
    def do_index(self, job_manager, batch_size, ids, mode):

        client = MongoClient(**self.mongo_client_args)
        database = client[self.mongo_database_name]
        collection = database[self.mongo_collection_name]

        if ids:
            self.logger.info(
                (
                    "Indexing from '%s' with specific list of _ids, "
                    "create indexer job with batch_size=%d."
                ),
                self.mongo_collection_name, batch_size
            )
            # use user provided ids in batch
            id_provider = iter_n(ids, batch_size)
        else:
            self.logger.info(
                (
                    "Fetch _ids from '%s', and create "
                    "indexer job with batch_size=%d."
                ),
                self.mongo_collection_name, batch_size
            )
            # use ids from the target mongodb collection in batch
            id_provider = id_feeder(collection, batch_size, logger=self.logger)

        jobs = []  # asyncio.Future(s)
        error = None  # the first Exception

        total = len(ids) if ids else collection.count()
        schedule = Schedule(total, batch_size)

        def batch_finished(future):
            nonlocal error
            try:
                schedule.finished += future.result()
            except Exception as exc:
                self.logger.warning(exc)
                error = exc

        for batch_num, ids in zip(schedule, id_provider):
            yield from asyncio.sleep(0.0)

            # when one batch failed, and job scheduling has not completed,
            # stop scheduling and cancel all on-going jobs, to fail quickly.

            if error:
                for job in jobs:
                    if not job.done():
                        job.cancel()
                raise error

            self.logger.info(schedule)

            pinfo = self.pinfo.get_pinfo(
                schedule.suffix(self.mongo_collection_name))

            job = yield from job_manager.defer_to_process(
                pinfo, dispatch,
                self.mongo_client_args,
                self.mongo_database_name,
                self.mongo_collection_name,
                self.es_client_args,
                self.es_blkidx_args,
                self.es_index_name,
                ids, mode, batch_num
            )
            job.add_done_callback(batch_finished)
            jobs.append(job)

        self.logger.info(schedule)
        yield from asyncio.gather(*jobs)

        schedule.completed()
        self.logger.notify(schedule)
        return {"count": total}

    @asyncio.coroutine
    def post_index(self, *args, **kwargs):
        ...


class ColdHotResult(UserDict):

    def merge(self, result):
        for index, count in result.items():
            self.setdefault(index, 0)
            self[index] += count

class ColdHotIndexer():
    """
    This indexer works with 2 mongo collections to create a single index.
    - one premerge collection contains "cold" data, which never changes (not updated)
    - another collection contains "hot" data, regularly updated
    Index is created fetching the premerge documents. Then, documents from the hot collection
    are merged by fetching docs from the index, updating them, and putting them back in the index.
    """

    def __init__(self, build_doc, indexer_env, index_name):
        hot_build_doc = _BuildDoc(build_doc)
        cold_build_doc = hot_build_doc.extract_coldbuild()

        self.hot = Indexer(hot_build_doc, indexer_env, index_name)
        self.cold = Indexer(cold_build_doc, indexer_env, self.hot.es_index_name)

    @ asyncio.coroutine
    def index(self,
              job_manager,
              steps=["index", "post"],
              batch_size=10000,
              ids=None,
              mode="index"):
        """
        Same as Indexer.index method but works with a cold/hot collections strategy: first index the cold collection then
        complete the index with hot collection (adding docs or merging them in existing docs within the index)
        """
        assert job_manager
        if isinstance(steps, str):
            steps = [steps]

        result = ColdHotResult()
        if "index" in steps:
            # ---------------- Sebastian's Note ---------------
            # selectively index cold then hot collections, using default index method
            # but specifically 'index' step to prevent any post-process before end of
            # index creation
            # Note: copy backend values as there are some references values between cold/hot and build_doc
            cold_task = self.cold.index(job_manager, steps=("pre", "index"), batch_size=batch_size, ids=ids, mode=mode)
            result.merge((yield from cold_task))
            hot_task = self.hot.index(job_manager, steps=("index",), batch_size=batch_size, ids=ids, mode="merge")
            result.merge((yield from hot_task))
        if "post" in steps:
            # use super index but this time only on hot collection (this is the entry point, cold collection
            # remains hidden from outside)
            yield from self.hot.post_index()

        return result


class IndexManager(BaseManager):

    # An index config is considered a "source" for the manager
    # Each call returns a different instance from a factory call

    DEFAULT_INDEXER = Indexer

    def __init__(self, *args, **kwargs):
        """
        An example of config dict for this module.
        {
            "indexer_select": {
                None: "hub.dataindex.indexer.DrugIndexer", # default
                "build_config.cold_collection" : "mv.ColdHotVariantIndexer",
            },
            "env": {
                "prod": {
                    "host": "localhost:9200",
                    "indexer": {
                        "args": {
                            "timeout": 300,
                            "retry_on_timeout": True,
                            "max_retries": 10,
                        },
                        "concurrency": 3
                    },
                    "index": [
                        # for information only, only used in index_info
                        {"index": "mydrugs_current", "doc_type": "drug"},
                        {"index": "mygene_current", "doc_type": "gene"}
                    ],
                },
                "dev": { ... }
            }
        }
        """
        super().__init__(*args, **kwargs)
        self._srcbuild = get_src_build()
        self._config = {}

        self.logger, self.logfile = get_logger('indexmanager')

    # Object Lifecycle Calls
    # --------------------------
    # manager = IndexManager(job_manager)
    # manager.clean_stale_status() # in __init__
    # manager.configure(config)

    def clean_stale_status(self):
        IndexJobStateRegistrar.prune(get_src_build())

    def configure(self, conf):
        if not isinstance(conf, dict):
            raise TypeError(type(conf))

        # keep an original config copy
        self._config = copy.deepcopy(conf)

        # register each indexing environment
        for name, env in conf["env"].items():
            self.register[name] = env.get("indexer", {})
            self.register[name].setdefault("args", {})
            self.register[name]["args"].setdefault("hosts", env.get("host"))
            self.register[name]["name"] = name
        self.logger.info(self.register)

    # Job Manager Hooks
    # ----------------------

    def get_predicates(self):
        def no_other_indexmanager_step_running(job_manager):
            """IndexManager deals with snapshot, publishing,
            none of them should run more than one at a time"""
            return len([
                j for j in job_manager.jobs.values()
                if j["category"] == INDEXMANAGER_CATEGORY
            ]) == 0

        return [no_other_indexmanager_step_running]

    def get_pinfo(self):
        """
        Return dict containing information about the current process
        (used to report in the hub)
        """
        pinfo = {
            "category": INDEXMANAGER_CATEGORY,
            "source": "",
            "step": "",
            "description": ""
        }
        preds = self.get_predicates()
        if preds:
            pinfo["__predicates__"] = preds
        return pinfo

    # Hub Features
    # --------------

    def _select_indexer(self, target_name=None):
        """ Find the indexer class required to index target_name. """

        rules = self._config.get("indexer_select")
        if not rules or not target_name:
            self.logger.debug(self.DEFAULT_INDEXER)
            return self.DEFAULT_INDEXER

        # the presence of a path in the build doc
        # can determine the indexer class to use.

        path = None
        doc = self._srcbuild.find_one({"_id": target_name}) or {}
        for path_in_doc, _ in traverse(doc, True):
            if path_in_doc in rules:
                if not path:
                    path = path_in_doc
                else:
                    _ERR = "Multiple indexers matched."
                    raise RuntimeError(_ERR)

        kls = get_class_from_classpath(rules[path])
        self.logger.debug(kls)
        return kls

    def index(self,
              indexer_env,  # elasticsearch env
              target_name,  # source mongodb collection
              index_name=None,  # elasticsearch index name
              ids=None,  # document ids
              **kwargs):
        """
        Trigger an index creation to index the collection target_name and create an
        index named index_name (or target_name if None). Optional list of IDs can be
        passed to index specific documents.
        """

        indexer_env_ = dict(self[indexer_env])  # describes destination
        build_doc = self._srcbuild.find_one({'_id': target_name})  # describes source

        if not build_doc:
            raise ValueError("Cannot find build %s." % target_name)
        if not build_doc.get("build_config"):
            raise ValueError("Cannot find build config for '%s'." % target_name)

        idx = self._select_indexer(target_name)
        idx = idx(build_doc, indexer_env_, index_name)
        job = idx.index(self.job_manager, ids=ids, **kwargs)
        job = asyncio.ensure_future(job)
        job.add_done_callback(self.logger.debug)

        return job

    # TODO PENDING VERIFICATION
    def update_metadata(self,
                        indexer_env,
                        index_name,
                        build_name=None,
                        _meta=None):
        """
        Update _meta for index_name, based on build_name (_meta directly
        taken from the src_build document) or _meta
        """
        idxkwargs = self[indexer_env]
        # 1st pass we get the doc_type (don't want to ask that on the signature...)
        indexer = create_backend((idxkwargs["es_host"], index_name, None)).target_esidxer
        m = indexer._es.indices.get_mapping(index_name)
        assert len(m[index_name]["mappings"]) == 1, "Found more than one doc_type: " + \
            "%s" % m[index_name]["mappings"].keys()
        doc_type = list(m[index_name]["mappings"].keys())[0]
        # 2nd pass to re-create correct indexer
        indexer = create_backend((idxkwargs["es_host"], index_name, doc_type)).target_esidxer
        if build_name:
            build = get_src_build().find_one({"_id": build_name})
            assert build, "No such build named '%s'" % build_name
            _meta = build.get("_meta")
        assert _meta is not None, "No _meta found"
        return indexer.update_mapping_meta({"_meta": _meta})

    def index_info(self, remote=False):
        """ Show index manager config with enhanced index information. """
        # http://localhost:7080/index_manager

        async def _enhance(conf):
            conf = copy.deepcopy(conf)
            if remote:
                for env in self.register:
                    try:
                        client = AsyncElasticsearch(**self.register[env]["args"])
                        conf["env"][env]["index"] = [{
                            "index": k,
                            "aliases": list(v["aliases"].keys()),
                        } for k, v in (await client.indices.get("*")).items()]

                    except Exception as exc:
                        self.logger.warning(str(exc))
                    finally:
                        try:
                            await client.close()
                        except:
                            ...

            return conf

        job = asyncio.ensure_future(_enhance(self._config))
        job.add_done_callback(self.logger.debug)
        return job

    def validate_mapping(self, mapping, env):

        indexer = self._select_indexer()  # default indexer
        indexer = indexer(dict(mapping=mapping), self[env], None)

        self.logger.debug(indexer.es_client_args)
        self.logger.debug(indexer.es_index_settings)
        self.logger.debug(indexer.es_index_mappings)

        @asyncio.coroutine
        def _validate_mapping():
            client = AsyncElasticsearch(**indexer.es_client_args)
            index_name = ("hub_tmp_%s" % get_random_string()).lower()
            try:
                return (yield from client.indices.create(index_name, body={
                    "settings": (yield from indexer.es_index_settings.finalize(client)),
                    "mappings": (yield from indexer.es_index_mappings.finalize(client))
                }))
            finally:
                yield from client.indices.delete(index_name, ignore_unavailable=True)
                yield from client.close()

        job = asyncio.ensure_future(_validate_mapping())
        job.add_done_callback(self.logger.info)
        return job


class DynamicIndexerFactory():
    """
    In the context of autohub/standalone instances, create indexer
    with parameters taken from versions.json URL.
    A list of  URLs is provided so the factory knows how to create these
    indexers for each URLs. There's no way to "guess" an ES host from a URL,
    so this parameter must be specified as well, common to all URLs
    "suffix" param is added at the end of index names.
    """

    def __init__(self, urls, es_host, suffix="_current"):
        self.urls = urls
        self.es_host = es_host
        self.bynames = {}
        for url in urls:
            if isinstance(url, dict):
                name = url["name"]
                # actual_url = url["url"]
            else:
                name = os.path.basename(os.path.dirname(url))
                # actual_url = url
            self.bynames[name] = {
                "es_host": self.es_host,
                "index": name + suffix
            }

    def create(self, name):
        conf = self.bynames[name]
        pidxr = partial(ESIndexer, index=conf["index"],
                        doc_type=None,
                        es_host=conf["es_host"])
        conf = {"es_host": conf["es_host"], "index": conf["index"]}
        return pidxr, conf
