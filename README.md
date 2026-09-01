# Graph Doctor v0

A deliberately small, read-only integrity projection over one Hermes Kanban task neighborhood.

It does not own task state, copy a board, poll projects, or perform transitions. Kanban and Git remain canonical. The doctor opens one board database in SQLite read-only mode, seeds from one task or event, reads only that task plus its direct parents and children, filters routine heartbeats from the causal timeline, and emits deterministic JSON.

## Current invariant

`competing_review_paths` is raised when a task requests same-card review while also having a downstream review task. Hermes supports either topology, but using both duplicates review and can strand or crash the closure path.

An active conflict exits with status `2` and derives `integration_locked`. A conflict already reconciled by completing the implementation remains as a warning in the causal history.

## Usage

```sh
python3 graph_doctor.py \
  --db /absolute/path/to/kanban.db \
  --event-id 1562
```

or:

```sh
python3 graph_doctor.py \
  --db /absolute/path/to/kanban.db \
  --task-id t_a9f459d4 \
  --compact
```

Exit status:

- `0`: no active integrity error in the affected neighborhood;
- `1`: invalid input or unreadable board;
- `2`: active integrity error; deterministic transitions should stop.

## Verification

```sh
python3 -m unittest -v test_graph_doctor.py
```

The fixture reproduces the Saturn Office failure shape: an implementation task enters same-card review despite already owning an independent downstream review node.

## Explicit limits

- No board or Git mutation.
- No daemon, cache, snapshot corpus, graph database, or resident model context.
- One-hop affected subgraph only.
- One proven invariant only.
- No automatic transition or cognition routing yet.
