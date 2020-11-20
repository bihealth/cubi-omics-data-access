#!/bin/bash

[[ "$VERBOSE" == "1" ]] && set -x

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd $DIR/..

ENV_INGEST=${ENV_INGEST:-../env-ingest}
if [[ -z "$VIRTUAL_ENV" ]]; then
    source $ENV_INGEST/bin/activate
fi

export CELERY_BROKER_URL=redis://127.0.0.1:6379/0
export PYTHONPATH=`pwd`

ZONE=${ZONE:-omicsTestingZone}
SITE=test-site
YEAR=${YEAR:-2020}
DEVICE=${DEVICE:-M06205}
FOLDER=${FOLDER:-200602_M06205_0009_000000000-CW9PR}
DEST="/$ZONE/$SITE/raw-data/$YEAR/$DEVICE/$FOLDER"

SRC=${SRC-utils/example/200602_M06205_0009_000000000-CW9PR}

imkdir -p "$DEST"

python \
    -m irods_capability_automated_ingest.irods_sync \
    start \
        "$SRC" \
        "$DEST" \
        --synchronous \
        --progress
