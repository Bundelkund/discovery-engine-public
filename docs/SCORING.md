# Scoring System

## Ueberblick

Discovery Engine scored Jobs in 2 Stufen. Stage 1 ist schnell und regelbasiert (jeder Job). Stage 2 ist teuer und semantisch (nur Top-Kandidaten). Alles wird pro User-Profil berechnet.

```
Job + Profil → Stage 1 (Keyword) → Score 0-100
                                        │
                            Score >= 40? │ Ja: Store in DB
                                        │
                            Score >= 50? │ Ja: → Stage 2 (Embedding) → score_stage_2
```

## Stage 1: Keyword Scorer

**Datei:** `app/scoring/keyword.py`
**Config:** `config/scoring.yaml`

### Die 5 Dimensionen

| # | Dimension | Gewicht | Was sie misst | Datenquelle |
|---|-----------|---------|--------------|-------------|
| 1 | Archetype Match | 40 | Passt der Job zu den gewaehlten Archetypen? | `profiles.archetypes` (Gewichtung) × `config/archetypes.yaml` (Keywords) |
| 2 | Keyword Positive | 30 | Enthaelt der Job gewuenschte Begriffe? | `profiles.keywords_positive_tech` + `_soft` |
| 3 | Seniority | 10 | Passt das Level? | `profiles.seniority_boost` / `_penalty` |
| 4 | Remote Bonus | 5 | Ist Remote/Hybrid moeglich? | Hardcoded: "remote", "hybrid", "homeoffice" |
| 5 | Noise Penalty | -15 | Enthaelt der Job ungewuenschte Begriffe? | `profiles.keywords_negative` |

### Formel (Beispiel)

```
Job: "Senior AI Implementation Lead - Remote (Berlin)"
Profil: archetypes={bridge-builder: 0.9}, keywords_positive=["AI", "Implementation"]

1. Archetype Match:
   bridge-builder keywords: ["AI Implementation Lead", "AI Adoption"]
   Matches: 1 ("AI Implementation Lead" in title)
   Score: min(100, 1 × 40) × 0.9 = 36
   Beitrag: 36 × 40/100 = 14.4

2. Keyword Positive:
   Matches: 2 ("AI", "Implementation")
   Score: min(100, 2 × 25) = 50
   Beitrag: 50 × 30/100 = 15.0

3. Seniority:
   "Senior" in boost list → 100
   Beitrag: 100 × 10/100 = 10.0

4. Remote:
   "Remote" found → 100
   Beitrag: 100 × 5/100 = 5.0

5. Noise: keine negative Keywords → 0

TOTAL: 14.4 + 15.0 + 10.0 + 5.0 = 44.4 → score_stage_1 = 44
```

### Thresholds

| Threshold | Wert | Konfig-Key | Bedeutung |
|-----------|------|-----------|-----------|
| Store | 40 | `scoring.store_threshold` | Jobs unter 40 werden nicht gespeichert |
| Stage 2 Gate | 50 | `scoring.stages[1].gate_threshold` | Nur Jobs >= 50 bekommen Embedding-Score |

## Stage 2: Embedding Scorer

**Datei:** `app/scoring/embedding.py`
**Voraussetzung:** `profiles.cv_embedding` muss gesetzt sein (OpenAI Embedding des CV-Texts)

Berechnet Cosine Similarity zwischen Job Description Embedding und CV Embedding.
Ergebnis: `score_stage_2` (0-100, float).

Ohne CV Embedding wird Stage 2 uebersprungen — kein Fehler, Score bleibt bei Stage 1.

## Kalibrierung

### Gewichte aendern

**Datei:** `config/scoring.yaml`

```yaml
scoring:
  store_threshold: 40    # Minimum Score zum Speichern
  stages:
    - scorer_id: "keyword"
      stage: 1
      weights:
        archetype_match: 40   # Wie wichtig ist Archetype-Fit?
        keyword_positive: 30  # Wie wichtig sind explizite Keywords?
        seniority: 10         # Wie wichtig ist das Level?
        remote_bonus: 5       # Wie wichtig ist Remote?
        noise_penalty: -15    # Wie stark sollen Ausschluesse wirken?
    - scorer_id: "embedding"
      stage: 2
      gate_threshold: 50     # Ab welchem Stage-1-Score wird Stage 2 getriggert?
```

**Anpassung:** YAML aendern, Server neustarten. Kein Code-Change noetig.

### Archetype-Katalog erweitern

**Datei:** `config/archetypes.yaml`

Jeder Archetype hat Keywords auf Deutsch und Englisch. Der Scorer sucht diese Keywords im Job-Titel + Description. Mehr Keywords = mehr Treffer.

```yaml
archetypes:
  bridge-builder:
    label: "Bridge Builder"
    keywords_de: ["AI Adoption Lead", "Digitalisierungsmanager", ...]
    keywords_en: ["AI Implementation Lead", "Forward Deployed Engineer", ...]
```

**Anpassung:** Keywords hinzufuegen, Server neustarten.

### Profil-Keywords anpassen

Ueber die API oder direkt in Supabase:

```bash
curl -X PUT http://localhost:8092/profiles/{id} \
  -H "X-Api-Key: de-local-dev-key" \
  -H "Content-Type: application/json" \
  -d '{"archetypes": {"bridge-builder": 0.9, "trainer": 0.8}}'
```

Die `keywords_positive_tech` und `keywords_positive_soft` werden in Supabase direkt editiert oder ueber WonderApply Onboarding gesetzt.

### Score-Verteilung pruefen

```sql
-- Score-Verteilung nach Kalibrierung
SELECT score_stage_1, count(*) 
FROM jobs 
WHERE profile_id = '...' 
GROUP BY 1 ORDER BY 1 DESC;

-- Top Treffer
SELECT title, company, score_stage_1, archetype 
FROM jobs 
WHERE profile_id = '...' 
ORDER BY score_stage_1 DESC LIMIT 20;

-- Score-Breakdown pro Job (braucht App-Logs, Level DEBUG)
```

### Iterativer Kalibrierungs-Prozess

1. Scrape ausfuehren (`POST /scrape/greenhouse`)
2. Top 20 Jobs anschauen — sind die relevant?
3. Score-Verteilung pruefen — zu viele Low-Score Jobs?
4. Gewichte in `scoring.yaml` anpassen
5. Re-Score: `POST /score/batch` (scored alle unscored Jobs neu)
6. Wiederholen bis zufrieden

## Bekannte Limitierungen

- **Archetype-Keywords sind global** — alle User teilen denselben Katalog. Fuer User mit anderen Berufsfeldern muessen Keywords in archetypes.yaml erweitert werden. Langfristig: `custom_archetype_keywords` pro Profil (WonderApply Feature-Request).
- **keywords_positive in DB aufgeteilt** — `keywords_positive_tech` + `keywords_positive_soft` werden zur Scoring-Zeit gemergt via `app/utils/profile_mapper.py`.
- **Stage 2 braucht CV Embedding** — Muss manuell via OpenAI API erstellt und in `profiles.cv_embedding` gespeichert werden. Kein automatischer Trigger bei CV-Upload.
- **Kein Score-Explain Endpoint** — Man kann nicht sehen WARUM ein Job einen bestimmten Score hat (welche Dimension wie viel beigetragen hat). Die Details werden berechnet aber nicht persistiert.
