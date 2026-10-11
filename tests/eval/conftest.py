# The seed projects under fixtures/ carry their own pytest files, some of which
# fail on purpose; they are inputs to the eval harness, not tests of this repo.
collect_ignore = ["fixtures"]
