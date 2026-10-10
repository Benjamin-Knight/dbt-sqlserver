`full_refresh_build: prebuilt` now applies masks to the empty table before its clustered index and the load, so a mask on a clustered-index key column no longer fails a fresh build or full refresh.
