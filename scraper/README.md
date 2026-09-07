# Scraper — meetmete allikate elav jälgimine

Hoiab meetmete ametlikud allikad ajakohasena ja **viib need LLM-i konteksti**.
Kaks eri asja, mis on tahtlikult eraldatud:

| Kiht | Uueneb | Kes kinnitab |
|---|---|---|
| **Tõendikiht** — allikatekst, ekstraheeritud faktid, vastuolud | automaatselt | keegi; need on tsitaadiga tõendid, mitte kuvatavad väärtused |
| **Kuvatavad CSV väärtused** | käsitsi | inimene, `python -m scraper.review` kaudu |

Nii uueneb see, mida mudel *teab*, iga tsükliga, aga see, mida kasutajale
*näidatakse*, muutub ainult inimese kinnitusel.

**Scraper ei kirjuta kunagi master CSV-i ega eemalda sealt ridu.**

## Mida jälgitakse

Iga meetme kohta master CSV-st:

- `Link` veerg — `eis.ee` (15) või `eurekanetwork.org` (2)
- `Täiendavad lingid` veerg — `riigiteataja.ee` õigusaktid (13 unikaalset)

### Riigi Teataja on erijuhtum

Akti lehed on Angular SPA — tavaline HTML-i tõmbamine annab 35 tähemärki
tühja skeletti. Sisu tuleb avalikust API-st:

```
GET /public-api/api/v1/akt/{id}?leiaKehtiv=true   ->  {"kehtivId": ..., "aktiParameetrid": {"pealkiri": ...}}
GET /public-api/api/v1/akt/{kehtivId}/blob-html   ->  terviktekst
```

Master CSV lingib akti ID-le, mis oli kehtiv CSV koostamise ajal, ja
õigusakti muutmisel saab uus tekst UUE ID. **Skreeper loeb alati kehtivat
redaktsiooni** — `kehtivId` lahendatakse ja terviktekst tõmmatakse selle
järgi. Sama päring annab ka akti pealkirja (`act_title`), lisapäringut ei ole.

`kehtivId != lingitud id` EI ole seega vastuolu: nii faktid, vestlus kui
võrdlused käivad kehtiva teksti järgi. Kasutajale näidatakse lihtsalt, MILLIST
redaktsiooni ta vaatab — neutraalne rida „ℹ Õigusakt: <pealkiri> — kehtiv
redaktsioon X, jõustunud Y" (`backend/sources.py: _legal_act()`). Varem tekitas
iga muudetud akt hoiatuse `õigusakt_muutunud`; seda kandis 10 akti 14-st ja see
mattis päris vastuolud enda alla.

Erand on **kinnistatud** link. `?leiaKehtiv` paneb Riigi Teataja klõpsajale ise
kehtiva teksti serveerima; ilma selleta jääb klõpsaja päriselt kehtetule
tekstile. Master-CSV 13 lingist kannavad 10 seda parameetrit.
`riigiteataja.pins_revision()` eristab neid:

| väli | millal | severity | jõuab ülevaatusse |
|---|---|---|---|
| `õigusakt` | kinnistatud link + akti muudetud | kõrge | jah |

See on kaebus CSV lingi, mitte akti sisu kohta, ja tsitaat ütleb seda välja
(„Rakendus loeb andmed kehtivast redaktsioonist X"). `source_url` osutab
KEHTIVALE redaktsioonile — vanale osutamine jättis mulje, nagu tugineks
rakendus aegunud tekstile.

Vana redaktsiooni tekst ja kehtivuse lõpu kuupäev (`linked_text`,
`valid_until`) salvestatakse endiselt tõendina, aga UI-sse ega vestlusprompti
need ei lähe.

Vastuolud arvutatakse ümber IGAL tsüklil, ka siis, kui ükski leht ei muutunud
(`run.refresh_conflicts()`, faktid andmebaasist, LLM-kutsungit ei tehta).
`õigusakt`-vastuolu ei sõltu üldse LLM-i faktidest, vaid `page_state`-ist ja
meie võrdlusloogikast — ainult muutuse peale arvutades jäid ekraanile vana
koodiversiooni tsitaadid. `db.sync_pending_changes()` hoiab ülevaatuse töölaua
nendega kooskõlas: ümber liigitunud vastuolu ootel rida kustub, inimese juba
otsustatud read jäävad ajaloona alles.

## Kasutus

```bash
python -m scraper.run       # üks tsükkel käsitsi
python -m scraper.review    # ootel muudatuste ülevaatus (CLI)
```

Uusi sõltuvusi ei ole — ainult stdlib (`urllib`, `html.parser`, `sqlite3`).

### Ajastamine

Taustalõim `backend/main.py`-s, mitte cron. Väravaks keskkonnamuutuja:

```bash
SCRAPER_ENABLED=1
SCRAPER_INTERVAL_DAYS=7
```

Lõim ärkab kord tunnis ja loeb `meta.last_run_at`; tsükkel käivitub ainult
siis, kui viimasest jooksust on möödas ≥ `SCRAPER_INTERVAL_DAYS`. See on
oluline, sest `uvicorn --reload` käivitub arenduses kümneid kordi päevas.
Jooks "võetakse enda alla" `BEGIN IMMEDIATE` all, nii et mitu töölist ei
jookse korraga.

## Kulukontroll

LLM-i kutsutakse **ainult siis, kui mõni meetme allikas tegelikult muutus**
(või kui meede on esimest korda nähtud), ja siis **üks kutsung meetme kohta**,
mitte lehe kohta.

- esimene jooks: ~17 kutsungit (alusjoon, ühekordne)
- muutumatu nädal: **0 kutsungit**

Muutuse signaal on puhastatud teksti sha256, mitte toore HTML-i oma:
`eis.ee` ei tagasta `ETag`-i ja Cloudflare vahetab e-posti obfuskeerimise
tokeneid iga päringuga, nii et toore HTML-i hash ei ole kunagi stabiilne.

## Kuidas scrapetu jõuab mudelini

`backend/sources.py` loeb `data/scraper_state.db` **ainult-lugemisrežiimis**:

- `rank_block(measure)` → soovituste promptis, ~600 tähemärki meetme kohta:
  ainult värskuse kuupäev ja vastuolud. Täisteksti siia ei panda — see prompt
  hoiab korraga kõiki kandidaate.
- `chat_sources(measure)` → vestluse promptis **terve kehtiv õigusakt** +
  meetme leht (~60 000 tähemärki). Siin maksab scrapimine end tagasi: nõustaja
  saab küsida "kas välisriigi teenusepakkuja on abikõlblik?" ja mudel vastab
  § viitega. Õigusakti ei kärbita kunagi; eelarve ületamisel kärbitakse lehte.
- `card_status(measure)` → UI kaardil värskus ja punane vastuoluhoiatus.

Kui `data/scraper_state.db` puudub, tagastavad kõik need tühja väärtuse ja
rakendus käitub täpselt nagu enne scraperi lisamist.

## Failid

| Fail | Vastutus |
|---|---|
| `config.py` | teed, User-Agent, eelarved, kõrge riskiga väljad |
| `robots.py` | robots.txt hindaja (metamärgid, grupivalik) |
| `fetch.py` | robots-kontroll, rate limit, tingimuslik GET, HTML puhastus |
| `riigiteataja.py` | SPA möödaviik: kehtiva redaktsiooni lahendamine + tekst |
| `db.py` | SQLite: `page_state`, `measure_facts`, `source_conflicts`, `pending_changes`, `meta` |
| `extract.py` | LLM-ekstraheerimine + **deterministlik** vastuolude leidmine |
| `run.py` | tsükli orkestreerija |
| `review.py` | ootel muudatuste CLI |
| `scheduler.py` | taustalõim |

### Kaks lõksu, mis on koodis kommenteeritud

1. **User-Agent peab olema puhas ASCII.** Cloudflare vastab 403-ga IGALE
   päringule, kui UA sisaldab täpitähti. Viga näeb välja nagu robots.txt
   blokeering ja on tülikas diagnoosida.
2. **`urllib.robotparser` ei sobi nende allikate jaoks.** See kohtleb tühja
   rida kirje lõpuna (eis.ee robots.txt-s on tühje ridu `User-agent: *` grupi
   sees → kõik reeglid jäävad orvuks) ega toeta metamärke tee keskel
   (`*/kohtulahendid/*` ei kattu kunagi). Seetõttu on `robots.py`.

## Vastuolude leidmine on deterministlik

`extract.find_conflicts()` **ei kutsu LLM-i**. Summad võrreldakse
`measures.parse_amount()` kaudu (`'7 500'` == `'7500'`), protsendid
normaliseeritakse (`'20%'` == `'0.2'`), ja kui üks väärtus sisaldub teises
(`'avatud'` vs `'avatud taotlusvoor'`), ei ole see vastuolu. Proosaväljad
(`description`, `exclusions`, `checkpoints`) ei tekita vastuolu kunagi —
seal on ümbersõnastus reegel, mitte erand.

LLM-i ülesanne on ainult tekstist väärtuste **väljalugemine**, ja iga fakt
peab tulema koos VERBATIM tsitaadiga; tsiteerimata väärtus visatakse ära.

## Mida see EI tee

- Ei kontrolli VTA (vähese tähtsusega abi) jääki — see on taotleja-spetsiifiline
  üksikpäring riigiabi registrisse, mitte bulk-scrape.
- Ei avasta automaatselt uusi meetmeid (nõuaks laiemat crawlimist).
- Ei kirjuta master CSV-i ega eemalda sealt ridu.
