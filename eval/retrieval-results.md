# Retrieval evaluation

Run on 2026-11-19 over 23 labelled queries and 829 DarGlobal passages. Embeddings `BAAI/bge-small-en-v1.5`, reranker `Xenova/ms-marco-MiniLM-L-6-v2` (top 8 reranked). Regenerate with `uv run estatequery eval-retrieval`.

A passage is relevant when it contains the answer terms listed in `eval/retrieval.jsonl` (the facts of the answer, not the words of the query).

| Mode | P@4 | Hit@4 | Recall@10 | MRR@10 | NDCG@10 | Median ms | p95 ms |
|---|---|---|---|---|---|---|---|
| bm25 | 0.20 | 0.48 | 0.26 | 0.34 | 0.25 | 0 | 1 |
| dense | 0.24 | 0.65 | 0.49 | 0.54 | 0.43 | 5 | 5 |
| hybrid | 0.27 | 0.61 | 0.54 | 0.40 | 0.41 | 5 | 6 |
| hybrid+rerank | 0.41 | 0.78 | 0.54 | 0.66 | 0.54 | 139 | 169 |

### Keyword queries (14)

| Mode | P@4 | Hit@4 | Recall@10 | MRR@10 | NDCG@10 |
|---|---|---|---|---|---|
| bm25 | 0.27 | 0.64 | 0.34 | 0.44 | 0.33 |
| dense | 0.32 | 0.79 | 0.62 | 0.66 | 0.55 |
| hybrid | 0.38 | 0.79 | 0.72 | 0.52 | 0.55 |
| hybrid+rerank | 0.59 | 1.00 | 0.72 | 0.86 | 0.71 |

### Semantic queries (9)

| Mode | P@4 | Hit@4 | Recall@10 | MRR@10 | NDCG@10 |
|---|---|---|---|---|---|
| bm25 | 0.08 | 0.22 | 0.14 | 0.19 | 0.12 |
| dense | 0.11 | 0.44 | 0.30 | 0.35 | 0.25 |
| hybrid | 0.11 | 0.33 | 0.27 | 0.21 | 0.20 |
| hybrid+rerank | 0.14 | 0.44 | 0.27 | 0.36 | 0.29 |

### Rank of the first relevant passage per query

| Query | Relevant passages | bm25 | dense | hybrid | hybrid+rerank |
|---|---|---|---|---|---|
| ceo (keyword): Ziad El Chaar CEO | 2 | 1 | 1 | 1 | 1 |
| lse (keyword): London Stock Exchange listing | 7 | 1 | 1 | 1 | 1 |
| golden-visa (keyword): golden visa 2 million AED | 11 | 1 | 1 | 1 | 1 |
| astera (keyword): Aston Martin interiors Al Marjan Island | 7 | 3 | 2 | 2 | 1 |
| jeddah-completion (keyword): Trump Tower Jeddah completion date | 1 | 2 | - | 6 | 2 |
| missoni-spain (keyword): Missoni Costa del Sol villas | 6 | - | 2 | 3 | 1 |
| elie-saab-doha (keyword): Elie Saab Doha residences | 4 | 2 | 1 | 2 | 1 |
| mouawad-riyadh (keyword): Mouawad villas Riyadh | 5 | - | 2 | 5 | 2 |
| lamborghini-spain (keyword): Lamborghini villas Benahavis | 2 | - | 5 | 7 | 2 |
| mulliner (keyword): Mulliner Piccadilly apartment | 1 | - | 1 | 2 | 1 |
| tokenization (keyword): tokenization fractional ownership | 6 | - | 3 | 3 | 2 |
| w-dubai (keyword): W Residences Downtown Dubai | 10 | 2 | 1 | 3 | 1 |
| paramount (keyword): Paramount partnership | 3 | 1 | 1 | 1 | 1 |
| fendi-azure (keyword): Fendi Casa oceanfront villas | 8 | 3 | 5 | 4 | 1 |
| residency (semantic): can a foreign buyer get long-term residency by purchasing property | 5 | - | 7 | - | - |
| public-company (semantic): is the company publicly traded | 8 | 5 | 1 | 3 | 1 |
| beach-villas (semantic): waterfront houses facing the sea in Muscat | 7 | - | - | - | - |
| rentable (semantic): hotel managed apartments that earn rental income | 2 | 2 | 1 | 1 | 1 |
| who-leads (semantic): who runs the business | 1 | - | 2 | 5 | 1 |
| car-brands (semantic): which supercar manufacturers put their name on homes | 17 | 1 | 3 | 3 | 4 |
| penthouses (semantic): very expensive one-off mansions and penthouses | 7 | - | - | - | - |
| sold-out (semantic): which developments are already finished and no longer available | 11 | - | 5 | - | - |
| london (semantic): London property by the developer | 6 | - | - | - | - |

### Against BM25

- **dense**: MRR +0.20, Hit@4 +0.17, +5 ms median. Clears the 0.05 margin on MRR or Hit@4.
- **hybrid**: MRR +0.05, Hit@4 +0.13, +5 ms median. Clears the 0.05 margin on MRR or Hit@4.
- **hybrid+rerank**: MRR +0.32, Hit@4 +0.30, +139 ms median. Clears the 0.05 margin on MRR or Hit@4.

Latency is measured on the machine that ran this, not on the server; a 2-vCPU host is slower. Small differences between modes on 23 queries are not significant.
