# Rahastusnõustaja: hindamine ja soovitatav lähenemine

## Kuidas praegune hindamine töötab

Süsteem kasutab kaheastmelist toru: esmalt deterministlik eelfilter, seejärel üks LLM-kõne, mis järjestab ja skoorib.

### 1. Eelfilter (deterministlik, ilma LLM-ita)

`backend/measures.py` → `filter_candidates()` eemaldab meetmed, mis selgelt ei sobi:

- meede on suletud
- taotleja tüüp ei klapi (OÜ / MTÜ-SA / konsortsium)
- suurettevõte vs ainult VKE-dele mõeldud meede
- piirkond on välistatud (nt „Eesti, v.a Harju maakond…”)
- kasutaja tegevusala on välistatud sektorite hulgas

Filter on tahtlikult konservatiivne: kahtlase puhul meede jäetakse alles ja edasi saadetakse LLM-ile.

**Mida filter praegu ei arvesta:** eelarve vahemik, omafinantseering, projekti kirjeldus, täpsustavate küsimuste vastused.

### 2. Täpsustavad küsimused (deterministlik)

Esimesel API-kõnel (`clarifications: null`) genereeritakse küsimused reeglite põhjal — nt T&A, partner, AI, arengufaas — kui vähemalt ühel järelejäänud meetmel on vastav nõue.

Vastused edastatakse hinnangusse, kuid **ei muuda eelfiltri tulemust**.

### 3. LLM-järjestamine ja skoor (subjektiivne)

Teisel API-kõnel (`clarifications: {}` või vastused) kutsub `backend/rank.py` välja Claude’i ühe korraga:

- saadab kõik eelfiltrist läbinud meetmed koos struktureeritud andmetega ja detailfailide tsitaatidega
- palub valida kuni 5 parimat, anda igale skoor 1–5, kirjutada eestikeelne selgitus ja loetleda 2–4 kontrollkohta

Skooril **puudub arvutusvalem** — see on mudeli hinnang prompti põhjal.

### 4. Tulemuse kokkupanek

Mudeli vastus seotakse meetmetega normaliseeritud nime järgi. Faktid (toetus, omafinantseering, staatus, lingid) tulevad CSV-st, mitte mudelist. Tundmatu nimega tulemused visatakse ära.

---

## Probleemid praeguse lähenemisega

- Üks lai ülesanne (vali, järjesta, skoori, selgita, vali kontrollkohad) → suur varieeruvus eri mudelite ja käivituste vahel
- Skoor 1–5 on defineerimata — mudel otsustab vabalt
- Täpsustavad vastused ei mõjuta reeglipõhist filtrit ega skoori otse
- Eelarve ja omafinantseering on mudeli otsustada, kuigi need on reeglitega arvutatavad
- Kõik meetmed ühes suures promptis → positsiooniline kallutatus, halvem väikeste mudelite jaoks

---

## Soovitatav lähenemine: struktureeritud skoor + väike LLM-osa

**Põhimõte:** numbrid arvutab kood; mudel täidab ainult lünki, mida reeglid ei suuda (projekti kirjelduse semantiline sobivus).

```
Sisend → laiendatud eelfilter → deterministlik osaskoor → LLM (enum-vastused) → kood: koguskoor + sort → (valikuline) selgituse genereerimine
```

### 1. Laienda eelfiltri reegleid

Lisa pärast täpsustavate küsimuste vastuseid:

- eelarve vs `max_grant_eur`
- omafinantseering vs meetme nõue
- T&A / AI / partner / ülikool: kui meede nõuab ja kasutaja vastas „Ei”, eemalda või raske karistus

### 2. Deterministlik osaskoor (nt 0–60 punkti)

Iga meede saab fikseeritud dimensioonide kaudu punkte:

| Dimensioon | Allikas |
|------------|---------|
| Eelarve sobivus | `budget_range` vs `max_grant_eur` |
| Omafinantseering | `cofinancing_pct` vs meetme % |
| T&A / AI / partner / ülikool | täpsustused + meetme väljad |
| Arengufaas | täpsustus vs meetme `phase` |
| Staatus | `tulemas` = väike karistus |
| Andmete kindlus | detailfaili `kindlus` väljad |

### 3. LLM ainult semantiliseks sobivuseks (nt 0–40 punkti)

Ühe meetme kaupa, identne mall, **enum-vastused**, mitte 1–5 skoor:

```json
{
  "project_type_fit": "yes|partial|no|unknown",
  "field_fit": "yes|partial|no|unknown",
  "purpose_fit": "yes|partial|no|unknown"
}
```

Kaardistus koodis: nt `yes=15`, `partial=8`, `no=0`, `unknown=5`.

Väikesed mudelid on stabiilsemad 3–5 enum-välja täitmisel kui tervikliku järjestamise tegemisel.

### 4. Lõplik skoor ja kuvamine koodis

```text
total = deterministic (0–60) + semantic (0–40) − staatus/kindlus karistused
```

UI-s võib kuvada endiselt 1–5, kuid see tuleb valemist, mitte mudeli otsusest:

| Total | Kuvamine |
|-------|----------|
| 85–100 | 5/5 |
| 70–84 | 4/5 |
| 55–69 | 3/5 |
| 40–54 | 2/5 |
| <40 | ära näita |

### 5. Eralda skoorimine selgitusest

1. **Skoorimise samm** — väike mudel, `temperature=0`, ainult JSON, ilma proosata
2. **Selgituse samm** — ainult top 3–5 tulemuse jaoks; suurem mudel võib kirjutada eestikeelse teksti juba arvutatud dimensioonide põhjal

Selgitus ei tohi skoori muuta.

### 6. Kontrollkohad reeglitest, mitte mudelist

- võta CSV `checkpoints` väljast fikseeritud arv
- lisa tingimuslikke punkte, kui mõni dimensioon sai `partial` või `unknown`

### 7. Meetmete metadata eelcompute (ühekordselt)

Detailfailidest lühike profiil meetme kohta (projekti tüübid, faasid, valdkonnad, nõuded). Skoorimine kasutab lühikest profiili, mitte pikki tsitaattabeleid.

### 8. Varieeruvuse vähendamine

- `temperature=0`
- JSON-skeemi valideerimine, deterministlik sort (`-total_score`, siis nimi)
- valikuline vahemälu: `hash(project_description + measure_id)`
- logi dimensioonide jaotus („miks 3/5?”)

---

## Mudelite jaotus

| Ülesanne | Mudel |
|----------|-------|
| Eelfilter + arvutuslik skoor | ilma LLM-ita |
| 3 enum semantilist kontrolli | väike/kiire mudel |
| Eestikeelne selgitus (top tulemused) | keskmine mudel (valikuline) |
| Metadata ekstraheerimine | suurem mudel üks kord offline |

---

## Rakendamise järjekord

1. Laienda eelfiltri reegleid (eelarve, omafinantseering, täpsustused)
2. Lisa deterministlik osaskoor Pythonis
3. Asenda „järjesta ja anna 1–5” → enum-rubric ühe meetme kaupa
4. Arvuta lõplik skoor koodis
5. Genereeri selgitused eraldi, ainult parimatele

See säilitab olemasoleva API vastuse kuju (`score`, `explanation`, `checks`), kuid muudab skoori taaskasutatavaks ja vähem sõltuvaks mudeli suurusest.
