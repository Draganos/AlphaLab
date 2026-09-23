#!/usr/bin/env python
"""Train `alpha_lab.ai.sklearn_sentiment.SklearnFinancialSentimentProvider`'s
real sentence-sentiment classifier on the real, published Financial
PhraseBank dataset (Malo et al. 2014) -- see `alpha_lab.ai.phrasebank`'s
own docstring for what the dataset is and its CC-BY-NC-SA-3.0 license.

TF-IDF + logistic regression: a standard, interpretable, well-established
approach for this exact task (real financial-domain text -> real sentiment
label), staying inside AlphaLab's existing "no heavy ML runtime dependency
unless the task genuinely needs one" discipline -- no transformer model,
no GPU, no external API.

Deliberately NOT run automatically by any other script or service: the
trained model artifact is a build step, explicitly triggered, exactly like
every other "explicit refresh" in this codebase. Neither the dataset nor
the trained model is committed to this repo (data/cache/, data/models/ are
both gitignored) -- re-running this script is how the model is obtained,
not `git pull`.
"""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import UTC, datetime  # noqa: E402

import joblib  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.metrics import classification_report  # noqa: E402
from sklearn.model_selection import train_test_split  # noqa: E402
from sklearn.pipeline import Pipeline  # noqa: E402
from sklearn.feature_extraction.text import TfidfVectorizer  # noqa: E402

from alpha_lab.ai.phrasebank import (  # noqa: E402
    DEFAULT_SUBSET,
    fetch_financial_phrasebank,
    parse_financial_phrasebank,
)
from alpha_lab.ai.sklearn_sentiment import DEFAULT_MODEL_PATH  # noqa: E402

RANDOM_STATE = 20140719  # the dataset's own "last modified" date (07/19/13-ish); fixed for reproducibility, not tuned


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subset", default=DEFAULT_SUBSET, help="Which agreement-level file to train on")
    parser.add_argument("--refresh", action="store_true", help="Re-fetch the dataset even if already cached")
    parser.add_argument("--model-path", default=str(DEFAULT_MODEL_PATH))
    parser.add_argument("--test-size", type=float, default=0.2)
    args = parser.parse_args()

    print(f"Fetching Financial PhraseBank ({args.subset})...")
    archive = fetch_financial_phrasebank(refresh=args.refresh)
    sentences = parse_financial_phrasebank(archive, subset=args.subset)
    print(f"Real labeled sentences: {len(sentences)}")

    texts = [item.text for item in sentences]
    labels = [item.label for item in sentences]

    train_texts, test_texts, train_labels, test_labels = train_test_split(
        texts, labels, test_size=args.test_size, stratify=labels, random_state=RANDOM_STATE,
    )

    pipeline = Pipeline([
        ("tfidf", TfidfVectorizer(ngram_range=(1, 2), min_df=2, sublinear_tf=True)),
        ("classifier", LogisticRegression(max_iter=2000, class_weight="balanced", random_state=RANDOM_STATE)),
    ])
    pipeline.fit(train_texts, train_labels)

    predictions = pipeline.predict(test_texts)
    report = classification_report(test_labels, predictions, output_dict=True)
    print(classification_report(test_labels, predictions))

    model_path = Path(args.model_path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "dataset": "Financial PhraseBank (Malo et al. 2014)",
        "dataset_subset": args.subset,
        "dataset_license": "CC-BY-NC-SA-3.0",
        "training_sentence_count": len(train_texts),
        "test_sentence_count": len(test_texts),
        "test_accuracy": report["accuracy"],
        "test_macro_f1": report["macro avg"]["f1-score"],
        "classes": list(pipeline.classes_),
        "trained_at": datetime.now(UTC).isoformat(),
        "random_state": RANDOM_STATE,
    }
    joblib.dump({"pipeline": pipeline, "metadata": metadata}, model_path)
    print(f"\nSaved trained classifier to {model_path}")
    print(json.dumps(metadata, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
