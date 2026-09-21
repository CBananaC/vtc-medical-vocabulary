# Vocabulary data layout

Bundled vocabulary is grouped by course and short session code:

```text
data/
├── HHS3190M/
│   ├── Anatomy/L1.json, L1-whoami.json
│   └── Physiology/L1.json–L6.json
├── HHS3892/
│   └── First-Aid/L1.json, L2.json
└── HHS4185/
    └── Common-Rehab/L1.json–L4.json, W1.json, W2.json, T1.json
```

Session codes are `L` = Lecture, `W` = Workshop, and `T` = Tutorial. The
number is the session number, so `W1.json` is Workshop 1 and `T1.json` is
Tutorial 1. The webapp reads the file list from `vocabulary-manifest.json`;
update that manifest when adding or moving a bundled dataset.

`_original/` contains exact pre-reorganization snapshots and is not included
in the manifest or loaded by the webapp.
