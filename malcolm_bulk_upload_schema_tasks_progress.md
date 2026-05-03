# edu_viz bulk upload schema refactor split

status: IN PROGRESS
current stage: task 51 schema/design + cancel/retry identity cleanup
last completed step: verified child-file Jobs grouping in pages.py and patched cancel route to resolve child_file_id first with fallback to legacy attempt-row ids
expected outputs:
- validated parent/child schema with latest-attempt linkage
- migration/backfill aligned to current Alembic chain
- retry flow and Jobs page grouped by child file identity
- validation evidence for live behavior
last verified activity: 2026-05-01 reviewed current model/router/template state, found cancel path still using old attempt ids, and patched it toward child-file identity
artifacts checked:
- /opt/edu_viz/app/models/bulk_ai_upload.py
- /opt/edu_viz/app/api/routers/bulk_ai_upload.py
- /opt/edu_viz/app/api/routers/pages.py
- /opt/edu_viz/app/templates/settings/jobs.html
- /opt/edu_viz/alembic/versions/0026_bulk_child_files.py
last completed item: job_worker exception cleanup now only force-fails latest processing child-file attempts plus legacy rows, preventing stale superseded attempts from being rewritten during fatal worker cleanup
next action: run final search/validation and determine whether the bulk upload schema refactor sweep is complete
