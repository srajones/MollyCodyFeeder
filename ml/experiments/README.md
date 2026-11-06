# Label-driven experiments (v0.6)

Offline, read-only. Need an extracted bundle in `exports/poor-puppy-ml-*/` and `ml/labels/feed_event_labels.json`.
Run with the dev venv (`uv venv --python 3.11 .venv && uv pip install -r requirements.txt scikit-learn`):

| Script | What it shows |
|---|---|
| `eval_audio.py` | Production analyzer vs owner labels: motor window, kibble coverage (flow.py), tings/s |
| `train_flow.py` | Leave-one-clip-out logistic regression for "kibble falling?" per window (numpy only) |
| `compare_models.py` | Same, logistic vs gradient boosting with multi-scale features (needs scikit-learn) |
| `event_features.py` | Per-feed features vs the owner's jam / normal labels |
| `ting_gaps.py` | Longest low-ting stretch inside the motor run, several window/threshold settings |

Findings on 29 labelled feeds (Oct 30 – Nov 5): see CHANGELOG v0.6 "Label-driven algorithm changes".
