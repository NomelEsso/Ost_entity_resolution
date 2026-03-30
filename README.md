# Ost_entity_resolution

## Overview
Matching datasets from SOK and Treasury to identify rightful owners of unclaimed properties.

## Project Structure
- `ost_entity_resolution.ipynb` - Main entity resolution notebook
- `citizenmatch-cleaning-dev.ipynb` - Data cleaning and preprocessing

## Workflow
1. **Data Ingestion** - Load SOK and Treasury datasets
2. **Data Cleaning** - Standardize and preprocess records
3. **Entity Matching** - Match records across datasets to resolve ownership
4. **Output** - Identify rightful owners of unclaimed properties

## Tech Stack
- Python (Jupyter Notebooks)
- Vertex AI Workbench
- Pandas / Data processing libraries

## Setup
```bash
git clone https://github.com/NomelEsso/Ost_entity_resolution.git
cd Ost_entity_resolution
```

## Notes
- Data sources: SOK and Treasury datasets
- Environment: Google Cloud Vertex AI
