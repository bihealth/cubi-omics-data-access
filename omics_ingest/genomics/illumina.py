"""Code with ingest capability logic for Illumina sequencer output.

- Use ``Operation.PUT_SYNC`` to update changed files in addition to uploading new ones.
- Handle initial upload and update of ``RunInfo.xml`` and ``runParameters.xml`` and update
  collection AVU.
- When a run folder is first seen write current timestamp to ``omics::ingest::first_seen``
- Every time a is changed then write current timestamp to ``omics::ingest::last_update``
- If the marker file for being done has been written out and ``omics::ingest::last_update``
  is longer than ``DELAY_UNTIL_AT_REST`` (e.g., 15 minutes) in the past then move away the
  run folder into the ingested part of the landing zone.
"""

import datetime
import pathlib
import typing

import dateutil.parser
from irods_capability_automated_ingest.core import Core
from irods_capability_automated_ingest.utils import Operation
from irods_capability_automated_ingest.sync_irods import irods_session

from ._illumina import (
    parse_runinfo_xml,
    parse_runparameters_xml,
    parse_netcopy_complete_txt,
    runparameters_to_marker_file,
    RunInfo,
    NetcopyInfo,
)
from ._helpers import cleanuping


#: This time should pass after the previous update and the existance of the output marker file
#: for a run folder to be considered at rest and moved away.
DELAY_UNTIL_AT_REST = datetime.timedelta(minutes=15)


def to_ingested_path(orig_path: str) -> str:
    """Convert a run folder path to an "ingested" path."""
    orig_path = pathlib.Path(orig_path)
    ingested_base = orig_path.parent.parent / (orig_path.parent.name + "-INGESTED")
    return ingested_base / orig_path.name


def apply_runinfo_metadata(session, run_info: RunInfo, target: str) -> None:
    """Apply ``RunInfo`` meta data to collection AVUs."""
    target_coll = str(pathlib.Path(target).parent)
    with cleanuping(session):
        coll = session.collections.get(target_coll)
        for avu in run_info.to_avus():
            coll.metadata.add(*avu)


def apply_runparameters_metadata(session, values: typing.Dict[str, str], target: str) -> None:
    """Apply ``runParameters.xml`` meta data to collection AVUs."""
    target_coll = str(pathlib.Path(target).parent)
    with cleanuping(session):
        coll = session.collections.get(target_coll)
        for key, value in values.items():
            coll.metadata.add(key, value, "")


def apply_netcopy_complete_metadata(session, netcopy_info: NetcopyInfo, target: str) -> None:
    """Apply netcopy complete meta data to collection AVUs."""
    with cleanuping(session):
        coll = session.data_objects.get(target)
        for avu in netcopy_info.to_avus():
            coll.metadata.add(*avu)


def _post_runinfoxml_create_or_update(logger, session, meta):
    """Actually parse the XML files and apply meta data."""
    path = meta["path"]
    if path.lower().endswith("/runinfo.xml"):
        logger.info("applying RunInfo.xml meta data from %s" % path)
        apply_runinfo_metadata(session, parse_runinfo_xml(path), meta["target"])
    elif path.lower().endswith("/runparameters.xml"):
        values = parse_runparameters_xml(path)
        logger.info("applying runParameters.xml meta data from %s (%d)" % (path, len(values)))
        apply_runparameters_metadata(session, values, meta["target"])
    elif "netcopy_complete" in path.lower():
        netcopy_info = parse_netcopy_complete_txt(path)
        if netcopy_info:
            logger.info("applying Netcopy_complete meta data from %s" % path)
            apply_netcopy_complete_metadata(session, netcopy_info, meta["target"])


#: AVU key to use for ``last_update`` attribute.
KEY_LAST_UPDATE = "omics::ingest::last_update"


def _post_create_or_update(logger, session, meta):
    """Update the ``last_update`` meta data value."""
    # Get path in irods that corresponds to root and update the meta data there.
    path = pathlib.Path(meta["path"])
    root = pathlib.Path(meta["root"])
    target = pathlib.Path(meta["target"])
    rel_path = path.relative_to(root)
    root_target = str(target)[: -(len(str(rel_path)) + 1)]
    with cleanuping(session) as session:
        coll = session.collections.get(root_target)
        # Remove all old meta data.
        for meta in coll.metadata.get_all(KEY_LAST_UPDATE):
            coll.metadata.remove(meta)
        # Add new meta data.
        coll.metadata.add(KEY_LAST_UPDATE, datetime.datetime.now().isoformat(), "")


#: AVU key to use for ``first_seen`` attribute.
KEY_FIRST_SEEN = "omics::ingest::first_seen"


class event_handler(Core):
    @staticmethod
    def pre_job(hdlr_mod, logger, meta):
        """Set the ``first_seen`` meta data value."""
        with cleanuping(irods_session(hdlr_mod=hdlr_mod, meta=meta, logger=logger)) as session:
            coll = session.collections.get(meta["target"])
            if not coll.metadata.get_all(KEY_FIRST_SEEN):
                coll.metadata.add(KEY_FIRST_SEEN, datetime.datetime.now().isoformat(), "")

    @staticmethod
    def post_job(hdlr_mod, logger, meta):
        with cleanuping(irods_session(hdlr_mod=hdlr_mod, meta=meta, logger=logger)) as session:
            coll = session.collections.get(meta["target"])
            last_update = None
            for irods_meta in coll.metadata.get_all(KEY_LAST_UPDATE):
                value = dateutil.parser.parse(irods_meta.value)
                if last_update is None or value > last_update:
                    last_update = value
            last_update_age = datetime.datetime.now() - (last_update or datetime.datetime.now())
            if last_update_age > DELAY_UNTIL_AT_REST:
                logger.info(
                    "age of last update of %s is %s (<%s) -- will move to ingest"
                    % (meta["target"], last_update_age, DELAY_UNTIL_AT_REST)
                )
                root = pathlib.Path(meta["root"])
                new_root = to_ingested_path(meta["root"])
                logger.info("attempting move %s => %s" % (root, new_root))
                try:
                    new_root.parent.mkdir(exist_ok=True)
                    root.rename(new_root)
                except OSError as e:
                    logger.error("could not move to ingest: %s" % e)
            else:
                logger.info(
                    "age of last update of %s is %s (<%s) -- not moving to ingest"
                    % (meta["target"], last_update_age, DELAY_UNTIL_AT_REST)
                )

    @staticmethod
    def operation(session, meta, **options):
        """Return ``Operation.PUT_SYNC`` to also put changed files."""
        return Operation.PUT_SYNC

    @staticmethod
    def post_data_obj_create(hdlr_mod, logger, session, meta, **options):
        """Update run folder meta data from ``RunInfo.xml`` and ``runParameters.xml`` files after
        initial upload and update."""
        _post_runinfoxml_create_or_update(logger, session, meta)
        _post_create_or_update(logger, session, meta)

    @staticmethod
    def post_data_obj_update(hdlr_mod, logger, session, meta, **options):
        """Update run folder meta data from ``RunInfo.xml`` and ``runParameters.xml`` files after
        initial upload and update."""
        _post_runinfoxml_create_or_update(logger, session, meta)
        _post_create_or_update(logger, session, meta)
