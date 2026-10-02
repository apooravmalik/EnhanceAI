# V1.1 RAG sweep specifications

Each file runs the same 2 × 2 × 2 grid against one immutable local dataset
snapshot:

- index storage: artifact or sqlite
- hashed-vector dimensions: 32 or 128
- reciprocal-rank-fusion constant: 10 or 60

Run a grid with:

    uv run aisys sweep sweeps/squad.yaml

The system definition is restored after every sweep. Each configuration gets
its own ingested index artifact and evaluation experiment.
