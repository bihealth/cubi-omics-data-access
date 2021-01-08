"""Common code for the omics ingest."""

from contextlib import contextmanager
import datetime
import os
import os.path
import pathlib
import subprocess
import typing

import dateutil.parser
from irods_capability_automated_ingest.sync_irods import irods_session
from irods.exception import CAT_SUCCESS_BUT_WITH_NO_INFO
from irods.meta import iRODSMeta

#: AVU key to use for ``last_update`` attribute.
KEY_LAST_UPDATE = "omics::ingest::last_update"
#: AVU key to use destionation run folder ingestion status.
KEY_STATUS = "omics::ingest::status"
#: AVU key to use for ``first_seen`` attribute.
KEY_FIRST_SEEN = "omics::ingest::first_seen"

#: Number of threads to run hashdeep with.
HASHDEEP_THREADS = 8
#: Algorithm to use for hashing.
HASHDEEP_ALGO = "md5"

#: File name for local manifest file.
MANIFEST_LOCAL = "_MANIFEST_LOCAL.txt"
#: File name for iRODS manifest file.
MANIFEST_IRODS = "_MANIFEST_IRODS.txt"


@contextmanager
def cleanuping(thing):
    try:
        yield thing
    finally:
        thing.cleanup()


def to_ingested_path(orig_path: typing.Union[str, pathlib.Path]) -> pathlib.Path:
    """Convert a run folder path to an "ingested" path."""
    orig_path = pathlib.Path(orig_path)
    ingested_base = orig_path.parent.parent / (orig_path.parent.name + "-INGESTED")
    return ingested_base / orig_path.name


def _compare_manifests(path_local, path_irods, dest_coll, logger):
    """Compare manifests at paths ``path_local`` and ``path_irods``.

    Consider paths in ``path_irods`` relative to ``dest_coll``.
    """
    # Load file sizes and checksums.
    info_local = {}
    with open(path_local, "rt") as inputf:
        for line in inputf:
            if line.startswith("#") or line.startswith("%"):
                continue
            line = line.strip()
            size, chksum, path = line.split(",")
            info_local[path] = (size, chksum)
    info_irods = {}
    with open(path_irods, "rt") as inputf:
        for line in inputf:
            if line.startswith("#") or line.startswith("%"):
                continue
            line = line.strip()
            size, chksum, path = line.split(",")
            path = path.replace(dest_coll, ".")
            info_local[path] = (size, chksum)

    # Compare file sizes and checksums.
    ok = True
    for path in info_local.keys() & info_irods.keys():
        size_local, chksum_local = info_local[path]
        size_irods, chksum_irods = info_irods[path]
        if size_local != size_irods:
            ok = False
            logger.error("file size does not match %s vs %s for %s", size_local, size_irods, path)
        if chksum_local != chksum_irods:
            ok = False
            logger.error(
                "file checksum does not match %s vs %s for %s", chksum_local, chksum_irods, path
            )
    # Find extra items on either side.
    extra_local = info_local.keys() - info_irods.keys()
    if extra_local:
        ok = False
        logger.error(
            "%d items locally that are not in irods, up to 10 shown:\n  %s"
            % (len(extra_local), "  \n".join(list(sorted(extra_local))[:10]))
        )
    extra_irods = info_irods.keys() - info_local.keys()
    if extra_irods:
        ok = False
        logger.error(
            "%d items in irods that are not present locally, up to 10 shown:\n  %s"
            % (len(extra_irods), "  \n".join(list(sorted(extra_irods))[:10]))
        )

    if not ok:
        raise RuntimeError("Difference in manifests!")


def _post_job_run_folder_done(
    logger,
    session,
    src_folder,
    dst_collection,
    is_folder_done: typing.Callable[[typing.Union[pathlib.Path, str]], bool],
    delay_until_at_rest,
):
    """Handle run folder being done:

    - Move into ingested folder on source.
    - Update status meta data in destination collection.
    """
    src_folder = pathlib.Path(src_folder)
    # Get "last updated" time from meta data.
    last_update = None
    for meta in dst_collection.metadata.get_all(KEY_LAST_UPDATE):
        value = dateutil.parser.parse(meta.value)
        if last_update is None or value > last_update:
            last_update = value
    last_update_age = datetime.datetime.now() - (last_update or datetime.datetime.now())
    # Do not proceed if not marked as done.
    if not is_folder_done(src_folder):
        logger.info("folder %s is not marked as done" % src_folder)
        return
    # Compute and check manifest and move if data is considered at rest.
    if last_update_age > delay_until_at_rest:
        logger.info(
            "age of last update of %s is %s (<%s) -- will finalize (manifest+move)"
            % (dst_collection, last_update_age, delay_until_at_rest)
        )
        # Compute local hashdeep manifest.
        local_path = os.path.join(src_folder, MANIFEST_LOCAL)
        logger.info("compute checksums and store to %s", chck_path)
        try:
            with open(local_path, "wt") as chk_f:
                p_find = subprocess.Popen(
                    ("find . -type f -and -not -path ./%s" % MANIFEST_LOCAL).split(" "),
                    cwd=src_folder,
                )
                subprocess.run(
                    (
                        "hashdeep -c %s -f /dev/stdin -j %s" % (HASHDEEP_ALGO, HASHDEEP_THREADS)
                    ).split(" "),
                    stdin=p_find.stdout,
                    stdout=chk_f,
                    encoding="utf-8",
                    check=True,
                )
                if p_find.wait() != 0:
                    raise subprocess.CalledProcessError(
                        "Problem running find: %s" % p_find.returncode
                    )
        except subprocess.CalledProcessError as e:
            logger.warn("Computing checksums failed, aborting: %s", e)
            os.remove(local_path)
            return
        # Compute manifest from irods checksums.
        logger.info("pull irods checksums into manifest")
        irods_path = os.path.join(src_folder, MANIFEST_IRODS)
        try:
            with open(irods_path, "wt") as chk_f:
                # Obtain information for files directly in destination collection.
                cmd = [
                    "iquest",
                    "%d,%s,%s/%s",
                    (
                        "SELECT DATA_SIZE, DATA_CHECKSUM, COLL_NAME, DATA_NAME "
                        "WHERE COLL_NAME = '%s' AND DATA_NAME != '%s' AND DATA_NAME != '%s'"
                    )
                    % (dst_collection, MANIFEST_LOCAL, MANIFEST_IRODS),
                ]
                subprocess.run(cmd, stdout=chk_f, encoding="utf-8", check=True)
                # Obtain information for files destination subcollections.
                cmd_sub = [
                    "iquest",
                    "%d,%s,%s/%s",
                    (
                        "SELECT DATA_SIZE, DATA_CHECKSUM, COLL_NAME, DATA_NAME "
                        "WHERE COLL_NAME like '%s/%'"
                    )
                    % dst_collection,
                ]
                subprocess.run(cmd_sub, stdout=chk_f, encoding="utf-8", check=True)
        except subprocess.CalledProcessError as e:
            logger.warn("Creation of iRODS manifest failed, aborting: %s", e)
            os.remove(irods_path)
            return
        # Compare the manifest files.
        _compare_manifests(local_manifest_dest, irods_manifest_dest, dst_collection, logger)
        # Put local hashdeep manifest.
        local_manifest_dest = os.path.join(dst_collection, MANIFEST_LOCAL)
        session.data_objects.put(local_path, local_manifest_dest)
        run_ichksum(local_manifest_dest)
        # Put manifest built from irods.
        irods_manifest_dest = os.path.join(dst_collection, MANIFEST_IRODS)
        session.data_objects.put(irods_path, irods_manifest_dest)
        run_ichksum(irods_manifest_dest)
        # Move folder.
        new_src_folder = to_ingested_path(src_folder)
        logger.info("attempting move %s => %s" % (src_folder, new_src_folder))
        try:
            new_src_folder.parent.mkdir(exist_ok=True)
            src_folder.rename(new_src_folder)
        except OSError as e:
            logger.error("could not move to ingested: %s" % e)
        # Update ``status`` meta data.
        dst_collection.metadata[KEY_STATUS] = iRODSMeta(KEY_STATUS, "complete", "")
    else:
        logger.info(
            "age of last update of %s is %s (<%s) -- not moving to ingested"
            % (dst_collection, last_update_age, delay_until_at_rest)
        )


def pre_job(hdlr_mod, logger, meta):
    """Set the ``first_seen`` meta data value."""
    src_root = pathlib.Path(meta["root"])
    with cleanuping(irods_session(hdlr_mod=hdlr_mod, meta=meta, logger=logger)) as session:
        dst_root = session.collections.get(meta["target"])
        dst_collections = {c.name: c for c in dst_root.subcollections}
        for src_folder in sorted([f.name for f in sorted(src_root.iterdir())]):
            if src_folder in dst_collections:
                coll = dst_collections[src_folder]
                if not coll.metadata.get_all(KEY_FIRST_SEEN):
                    coll.metadata[KEY_FIRST_SEEN] = iRODSMeta(
                        KEY_FIRST_SEEN, datetime.datetime.now().isoformat(), ""
                    )
            else:
                logger.info("Skipping %s as it corresponds to no destination collection")


def post_job(
    hdlr_mod,
    logger,
    meta,
    is_folder_done: typing.Callable[[typing.Union[pathlib.Path, str]], bool],
    delay_until_at_rest,
):
    """Move completed run folders into the "ingested" area."""
    src_root = pathlib.Path(meta["root"])
    with cleanuping(irods_session(hdlr_mod=hdlr_mod, meta=meta, logger=logger)) as session:
        dst_root = session.collections.get(meta["target"])
        dst_collections = {c.name: c for c in dst_root.subcollections}
        for src_folder in sorted([f.name for f in sorted(src_root.iterdir())]):
            if src_folder in dst_collections:
                _post_job_run_folder_done(
                    logger,
                    session,
                    src_root / src_folder,
                    dst_collections[src_folder],
                    is_folder_done,
                    delay_until_at_rest,
                )
            else:
                logger.info("Skipping %s as it corresponds to no destination collection")


def refresh_last_update_metadata(logger, session, meta):
    """Update the ``last_update`` and ``status`` meta data value."""
    # Get path in irods that corresponds to root and update the meta data there.
    path = pathlib.Path(meta["path"])
    root = pathlib.Path(meta["root"])
    target = pathlib.Path(meta["target"])
    rel_root_path = path.relative_to(root)  # relative to root
    rel_folder_path = "/".join(str(rel_root_path).split("/")[1:])  # relative to run folder
    root_target = str(target)[: -(len(str(rel_folder_path)) + 1)]
    with cleanuping(session) as session:
        coll = session.collections.get(root_target)
        # Replace ``last_update`` and ``status`` meta data.
        coll.metadata[KEY_LAST_UPDATE] = iRODSMeta(
            KEY_LAST_UPDATE, datetime.datetime.now().isoformat(), ""
        )
        coll.metadata[KEY_STATUS] = iRODSMeta(KEY_STATUS, "running", "")


def run_ichksum(irods_path: str) -> None:
    """Run ``ichksum $irods_path``."""
    subprocess.check_output(["ichksum", irods_path])
