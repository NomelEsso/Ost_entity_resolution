"""
BigQuery JavaScript UDFs for data cleaning.
"""

import logging

log = logging.getLogger(__name__)


_CLEAN_NAME_JS = r"""
    if (!val || val.trim() === '') return null;
    var v = val.normalize('NFD').replace(/[\u0300-\u036f]/g, '');
    v = v.toUpperCase();
    v = v.replace(/\b(MR|MRS|MS|MISS|DR|PROF|REV|SRGT|SGT|OFFICER|ATTY|ATTORNEY|JUDGE)\b/g, '');
    v = v.replace(/[^A-Z'\-\s]/g, ' ');
    v = v.replace(/\s+/g, ' ').trim();
    return v || null;
"""

_CLEAN_STREET_JS = r"""
    var parts = [val1, val2, val3].filter(function(p) { return p && p.trim(); });
    var v = parts.join(' ');
    if (!v.trim()) return null;
    v = v.normalize('NFD').replace(/[\u0300-\u036f]/g, '');
    v = v.toUpperCase();
    v = v.replace(/\bP\.?\s*O\.?\s*B(?:OX)?\b/g, 'PO BOX');
    v = v.replace(/\b(APARTMENT|APT)\b/g, 'APT');
    v = v.replace(/\b(SUITE|STE)\b/g, 'STE');
    v = v.replace(/#\s*([A-Z0-9\-]+)/g, 'APT $1');
    v = v.replace(/[.,]/g, '');
    var dirMap = {
        'NORTHWEST':'NW','NORTHEAST':'NE','SOUTHWEST':'SW','SOUTHEAST':'SE',
        'NORTH':'N','SOUTH':'S','EAST':'E','WEST':'W'
    };
    v = v.replace(/\b(NORTHWEST|NORTHEAST|SOUTHWEST|SOUTHEAST|NORTH|SOUTH|EAST|WEST)\b/g,
        function(m) { return dirMap[m]; });
    var stMap = {
        'STREET':'ST','AVENUE':'AVE','ROAD':'RD','LANE':'LN','DRIVE':'DR',
        'BOULEVARD':'BLVD','COURT':'CT','PLACE':'PL','PARKWAY':'PKWY',
        'CIRCLE':'CIR','HIGHWAY':'HWY'
    };
    v = v.replace(/\b(STREET|AVENUE|ROAD|LANE|DRIVE|BOULEVARD|COURT|PLACE|PARKWAY|CIRCLE|HIGHWAY)\b/g,
        function(m) { return stMap[m]; });
    v = v.replace(/[^A-Z0-9'\-\s]/g, ' ');
    v = v.replace(/\s+/g, ' ').trim();
    return v || null;
"""

_CLEAN_STATE_JS = r"""
    if (!val) return null;
    var v = val.normalize('NFD').replace(/[\u0300-\u036f]/g, '');
    v = v.toUpperCase().replace(/[^A-Z]/g, '').substring(0, 2);
    return v || null;
"""

_CLEAN_ZIP_JS = r"""
    if (!val) return null;
    var digits = String(val).replace(/\D/g, '').substring(0, 5);
    return digits || null;
"""



def create_cleaning_udfs(bq, dataset_ref):
    """Create all cleaning UDFs in the given dataset.

    Args:
        bq: BigQuery client
        dataset_ref: fully qualified dataset (e.g. "project.dataset")
    """
    log.info("Creating/updating cleaning UDFs in %s...", dataset_ref)

    # clean_name
    sql = (
        "CREATE OR REPLACE FUNCTION `%s.clean_name`(val STRING) "
        "RETURNS STRING "
        "LANGUAGE js AS r'''"
        "%s"
        "''';"
    ) % (dataset_ref, _CLEAN_NAME_JS)
    bq.query(sql).result()
    log.info("  ✅ clean_name")

    # clean_street
    sql = (
        "CREATE OR REPLACE FUNCTION `%s.clean_street`(val1 STRING, val2 STRING, val3 STRING) "
        "RETURNS STRING "
        "LANGUAGE js AS r'''"
        "%s"
        "''';"
    ) % (dataset_ref, _CLEAN_STREET_JS)
    bq.query(sql).result()
    log.info("  ✅ clean_street")

    # clean_state
    sql = (
        "CREATE OR REPLACE FUNCTION `%s.clean_state`(val STRING) "
        "RETURNS STRING "
        "LANGUAGE js AS r'''"
        "%s"
        "''';"
    ) % (dataset_ref, _CLEAN_STATE_JS)
    bq.query(sql).result()
    log.info("  ✅ clean_state")

    # clean_zip
    sql = (
        "CREATE OR REPLACE FUNCTION `%s.clean_zip`(val STRING) "
        "RETURNS STRING "
        "LANGUAGE js AS r'''"
        "%s"
        "''';"
    ) % (dataset_ref, _CLEAN_ZIP_JS)
    bq.query(sql).result()
    log.info("  ✅ clean_zip")

    log.info("✅ All cleaning UDFs created in %s", dataset_ref)
