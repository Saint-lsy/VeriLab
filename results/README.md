# Local experiment workspaces

This directory is reserved for project-specific VeriLab adapters and experiment workspaces. Its
contents are intentionally excluded from the VeriLab core repository so that datasets, model
checkpoints, predictions, Controller state, and challenge-specific provenance cannot be published
with the reusable application by accident.

The local checkout currently keeps the independent PANTHER and ISLES adapters here. Each adapter
retains its own Git history and should be versioned or published separately if desired. Do not use
`git add -f` to add their contents to the VeriLab core repository.

Controller state should remain outside this repository, for example under a dedicated
`verilab-state/<project-id>/` directory. Host-local compatibility symlinks may point into this
directory, but those links are not part of the core repository.
