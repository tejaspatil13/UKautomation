# Copilot

Runs the same search as the VO "Find a property" advanced search, finds the phone number for every listed
property, and produces an Excel file with **only the properties that have a phone number**.

It automates the manual workflow:
1. Search the VO site by town, postcode, description code and rateable value range.
2. Skip properties that were removed from the rating list; keep the ones showing a current valuation.
3. Search each remaining address for the business there and its phone number.
4. Put the properties that have a number into the spreadsheet.

Example: 50 properties found → 30 currently listed → 20 of those have a phone number → 20 rows in the Excel file.
No AI/LLM is used anywhere: it is database lookups plus explicit matching rules.

```
Dashboard ──▶ FastAPI ──▶ in-process job runner
                           ├─ VO rating list (local SQLite, official bulk download)
                           ├─ Places index (local SQLite, Overture Maps open data)
                           ├─ Matcher (postcode + building number + street score)
                           └─ Excel writer (client template) ──▶ download
```

## How the data is sourced (and why)

| Need | Source | Access method | Cost |
|---|---|---|---|
| Premises, address, rateable value | [VO compiled rating list](https://voaratinglists.blob.core.windows.net/html/rlidata.htm) (2026 list) | Official bulk download, imported into SQLite. No scraping of the "Find a property" site. | Free |
| Business name, phone | [Overture Maps places](https://docs.overturemaps.org/) (aggregates Meta, Microsoft, Foursquare, OSM and others) | Bulk GeoParquet, England & Wales extract imported into SQLite | Free |
| Registered company, owners | [Companies House bulk data](https://download.companieshouse.gov.uk/): Basic Company Data (monthly) + People with significant control snapshot | Downloaded part by part and imported into SQLite; matching needs no web requests | Free (OGL v3) |
| Directors / partners (active officers) | [Companies House](https://find-and-update.company-information.service.gov.uk/) | One request per *matched* company, remembered 30 days. Official API when `UKA_COMPANIES_HOUSE_API_KEY` is set, otherwise the public officers page | Free |

Why not scrape the VO site? It has no API. Results come 15 per page, pagination depends on the session, and the list
doesn't show RV, so one area search means hundreds of requests. The bulk file holds the same data
(the UARN in each site URL is the bulk file's key) and searches against it take milliseconds.

Why not Google? Google Search/Maps scraping is not permitted. The Places API puts phone numbers on its top-priced tier
(~$35/1,000 Text Search calls after 1,000 free/month), and its terms forbid caching or exporting Places content outside
Google's services. An exported call list is very likely outside those terms. The provider interface
(`app/contacts/base.py`) lets a licensed source be added later without touching the rest.

## Quick start (local)

Requires Python 3.12+ and Node 20+.

```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements-dev.txt
cd frontend && npm ci && npm run build && cd ..

cd backend
../.venv/bin/python -m app.voa.importer              # ~95 MB download, ~5 min import, 2.1M premises
../.venv/bin/python -m app.contacts.overture_import  # England & Wales places; time depends on bandwidth
../.venv/bin/python -m app.companies.bulk_import     # Companies House register + owners (~3 GB download, part by part)
../.venv/bin/uvicorn app.main:app --port 8000        # open http://localhost:8000
```

To work on the dashboard with hot reload, run `npm run dev` in `frontend/` (it proxies `/api` to port 8000).

## Using the dashboard

The form has the same fields, in the same order, as the VO site's advanced search:

1. **Town**, e.g. Coventry.
2. **Postcode**: full (`CV1 1FJ`), sector (`CV1 1`) or district (`CV1`). Enter a town, a postcode, or both.
3. **Description code**: the site's full list of 117 codes (e.g. `CS - Shop and Premises`), or Any.
4. **Rateable value from (£)**, e.g. 12000. Your existing sheets only contain RVs above £12,000.
5. **Rateable value to (£)**, blank for no limit.

Click **Start search**. The results table shows every listed property and why each one did or didn't get a number.
**Download Excel** gives only the ones with a phone number. If none has a number, no file is created and a message says so.

**Removed properties:** the data is the VO's *current* rating list, which contains only properties that are listed
with a valuation. Properties the site shows as "removed from the rating list on …" aren't in it, so they're skipped automatically.

**One row per business.** A business that occupies several rated entries is shown once, with every rateable value
listed and a Gov.uk record link for each.

**Business centres (4+ businesses in one building).** The business listings say who is in a building, never which
unit, so a rateable value is only given to a business when the unit is confirmed:
- **Unit confirmed:** Companies House has a company registered at the same unit number (the rating list writes
  "Unit 3.08", Companies House "Unit 308"). That company gets the unit, tagged **Unit confirmed · Companies House**,
  with its own phone when the listings have it (otherwise the building's number).
- **Occupier not known:** every other unit is left out of the results and the Excel file: there is no business
  name to call, and its rateable value is never credited to a guessed business. (The job message says how many
  units were left out.)

Buildings with two or three businesses keep the best guess, tagged **Shared building**. Re-running a search and
adding it to My sheet again refreshes rows that are already there.

**My sheet** collects businesses from many searches. **Download** asks for a file name and, optionally, how many
files to split it into (e.g. 10 or 15, one per caller); the rows are shared out evenly and you get a .zip.

**Get business owner names** (on My sheet) matches each business against the local copy of the Companies House
register. A company counts only when it is active, its name is similar, and its **registered office postcode is the
same** as the property's (or the business's own listed postcode). For each match the active officers (directors,
partners, members, secretaries; never resigned ones) are fetched once and remembered for 30 days; their names are
the "Owner / director". Owners with their shareholding come from the register. Matches go to **Potential customers**.
Many companies register at their accountant's address, so expect a minority to match (about 1 in 10 in a Coventry
test; fewer in central London).

Before the bulk data is imported, the app falls back to searching the Companies House website for every business.
Companies House blocks that after a few hundred requests, which is why the bulk data is used.

**Check for updates** (footer) checks all three data sources straight away and, if anything is newer, asks before
installing it.

If some lookups failed (a provider outage, for example), the job finishes as **completed with partial results**. The file can
still be downloaded, and **Retry failed parts** re-runs only the failed lookups.

## Output file

Based on Sheet1 of the client's workbook (`templates/output_template.xlsx`, generated by
`backend/scripts/build_template.py`, containing no client data):

| Col | Header | Content |
|---|---|---|
| A | business name | The business found at the property |
| B | add | The address **as the business lists it**; the VO address if no business was found |
| C | RV (2026 list) | Rateable value in £; several values (`£37,750 / £18,500`) when the business has several entries |
| D | Contact no | With country code, as text: `+44 20 7739 0399` |
| E, F | comments, Status | Left empty for the caller |
| G–P | Other numbers, Note, Description, Postcode, Google Maps, Gov.uk record, VO address, VO UARN, Search, Added | For checking the row |
| Q… | Gov.uk record 2, 3, … | One link per further rated entry of the same business |

The Potential customers file adds **Owner / director, Company name, Company number, Companies House (link) and
Registered office** after Status.

A second sheet, **About**, records the search criteria, counts, data sources, licence/attribution and the meaning of each status.
Cells that came from external data and start with `= + - @` are prefixed with `'` (formula-injection protection).

Things deliberately **not** reproduced: the Sheet3 bill calculator (its 49.3% multiplier is not a figure the tool can verify),
and the call-outcome comments (personal data).

## How a business is matched to a property

Each candidate business at the premises' postcode (or within 75 m when the source has no postcode) is scored:

| Evidence | Points |
|---|---|
| Same postcode | +50 (same district only +10) |
| No postcode on source, within 75 m of postcode centre | +35 |
| Building number matches / differs | +30 / −25 |
| Building name matches (premises without a number) | +20 |
| Street matches / similar / differs | +15 / +10 / −10 |
| VO firm name matches (rare) | +10 |
| Business type fits the VO description (e.g. takeaway at a "Shop and Premises") | +10 |
| Business type conflicts (e.g. an office business at a shop premises) | −15 |
| Source marks it permanently closed | −40 |
| Source confidence < 0.6 | −10 |

The highest-scoring business with a phone number (score ≥ 50) is used. Other businesses in the same building go in
**Other numbers at this address**, so if the first number reaches the wrong business the caller can try the next.
Shared office buildings are common, so check with the Google Maps link. No status label is shown; each row has a
one-line **Note** (e.g. "Same building number and postcode; 3 other business(es) at this address").
Advertising rights, bus shelters, car parks, masts, substations and similar are never given a phone number.
Every property with a phone number goes into the Excel file.

## Benchmark

Live runs on this machine (2026 VO list, Overture 2026-10-21 places, 27 Oct 2026):

| Search | Listed properties | With phone (in Excel) | Needs review | Time |
|---|---|---|---|---|
| Coventry, CV1 1FJ | 18 | 6 (all verified) | 7 | 0.1 s |
| Coventry, CV1, code CR (restaurants), RV from £12,000 | 73 | 29 (26 verified, 3 probable) | 44 | 5.8 s* |
| Coventry, CV1, RV from £12,000 | 1,178 | 360 (287 verified, 73 probable) | 634 | 1.4 s |

\*first query after a restart, cold disk cache.

Spot check against the client's hand-made sheet: 23 Trinity Street → Pepe's, 024 7622 3444, the same number the sheet has.

**Not yet measured:** overall accuracy against the client's ~2,800 hand-researched rows. Only 3 rows in that sheet link
to a VO page, so `scripts/benchmark_sample.py` must first be changed to match rows by postcode + building number.

About **a third of listed properties get a number**. The rest are mainly city-centre buildings with several businesses
at the same number (shop downstairs, offices upstairs) that the rules won't guess between, plus vacant or non-trading units.

## Deployment

**Windows desktop app.** The same application also ships as installable Windows software, with
the datasets on the client's own PC, a 2-day trial with remote activation, and automatic updates
from GitHub Releases. See [docs/desktop/DEVELOPER_GUIDE.md](docs/desktop/DEVELOPER_GUIDE.md) (build, develop, release),
[docs/desktop/LICENSING_GUIDE.md](docs/desktop/LICENSING_GUIDE.md), and for the client the
[installation](docs/desktop/WINDOWS_INSTALLATION_GUIDE.md) and [user](docs/desktop/CLIENT_USER_GUIDE.md) guides.

The app is a single Docker container that needs a **persistent disk** (≈2–3 GB: VO ≈ 0.7 GB + places + exports).
Serverless platforms (Vercel, Lambda) are **not suitable**: their filesystems are read-only or ephemeral, and both the datasets and the jobs need to persist.

**Render** (`render.yaml` blueprint): Docker web service, Starter plan, 5 GB disk at `/data`.
1. Push the repo to GitHub, then in Render choose *New → Blueprint* and select the repo.
2. Render generates `UKA_APP_PASSWORD` (see the service's Environment tab); log in with any username and that password.
3. On first boot `UKA_AUTO_IMPORT_ON_START=true` builds both datasets in the background. Searches return a clear
   "dataset not imported yet" message until the VO import finishes (~5 min).

**Any VPS / Fly.io / Railway**: `docker build -t uk-business-finder . && docker run -p 8000:8000 -v ukdata:/data --env-file .env uk-business-finder`.
Put HTTPS in front (Render/Fly do this automatically; on a VPS use Caddy or nginx with Let's Encrypt).

### Keeping the data up to date (automatic)

Once a day the app asks each source whether something new has been published. This is a small request; nothing
big is downloaded unless there is an update:
- **Government rating list:** the VO download page is checked for a newer file (new epoch number), and the file's
  server ETag is compared, so a republished file with the same name is also noticed.
- **Business listings:** Overture's list of releases is checked for a release newer than the one installed.
- **Companies House data:** the Basic Company Data release date (monthly); the owners snapshot is refreshed with it.

New data is downloaded and installed only between **01:00 and 05:00** (`UKA_REFRESH_WINDOW`), in a separate process.
The new data file is built alongside the old one and swapped in at the end, so searches keep working throughout.
If the internet is down or a download fails, the current data stays in place and the check runs again the next day.
The dashboard footer shows each dataset's version and when it was last checked.

The **Check for updates** button in the dashboard footer does the same check on demand and asks before installing.
Before installing, the app checks there is enough free disk space and refuses (keeping the current data) if not.

The server must be running at night for the automatic update to happen. To check or update by hand:
```bash
cd backend
../.venv/bin/python -m app.refresh --check   # what's installed vs what's published
../.venv/bin/python -m app.refresh --now     # install anything newer, right now
```

### Costs

| Item | Cost |
|---|---|
| VO bulk data, Overture places | Free (no API keys) |
| Render Starter + 5 GB disk | ~$7 + ~$1.25 per month |
| Optional Google Places (not enabled) | ~$20–35 per 1,000 lookups after the monthly free allowance, plus terms restrictions |

No API keys are needed in the default configuration. A free Companies House API key is optional.

## Configuration

See `.env.example`. All settings use the `UKA_` prefix: data directory, dashboard password, list year, refresh intervals,
result and concurrency limits, export retention (default 72 h, after which files and job records are deleted).

## Security

- No secrets reach the browser; the browser only calls this server's `/api`.
- Optional dashboard password (HTTP Basic, constant-time compare). Set it whenever the app is reachable from the internet.
- Pydantic validation on every input (postcode formats, allowed characters, RV ranges); errors come back as readable messages.
- Download names are generated server-side. Job IDs are validated 32-hex UUIDs, so user input never forms a path.
- Spreadsheet formula-injection sanitisation; security headers (CSP, nosniff, frame-deny); no stack traces in responses.
- Limits: 5,000 premises per search, 2 concurrent jobs, 10 queued, 72 h retention.

## Legal and compliance notes (read before use)

- **VO data licence**: the rating list may be used only for **non-domestic rating purposes**, i.e. helping occupiers
  (or their representatives / service providers) with their rates and valuations. Onward disclosure is limited to parties
  using it for the same purposes, and data must be deleted when no longer needed. The tool is configured for that use;
  using it for general sales prospecting would fall outside the licence.
- **PECR / TPS / CTPS**: unsolicited sales or marketing calls must be screened against the Telephone Preference Service
  (sole traders and partnerships) and the Corporate TPS (companies). **This tool does not screen numbers.** Screen them
  before calling.
- **UK GDPR**: sole traders' names and numbers are personal data. Keep the retention period short (the default deletes after 72 h).
- **Attribution**: Overture data must be credited (done in the About sheet).

## Known limitations

- The VO list describes **premises, not businesses**. Only 3.5% of entries name the occupier, so a business name
  comes only from the places match. Vacant units, multi-let buildings and recent changes of occupier show up as "No contact" or
  "Needs review".
- Open places data covers city-centre retail and hospitality better than industrial estates and offices.
- The VO bulk file updates about every 2 months (twice-weekly change files are not yet applied). A brand-new assessment can
  appear on the VO website before it appears here.
- Chains listed twice at one address under different names (e.g. "Games Workshop" with no phone and "Warhammer" with
  the phone) aren't linked, so the number is left blank rather than guessed.
- Phone numbers come from third-party listings and can be out of date. "Verified" means the listing matches the premises,
  not that the number was dialled.

## Tests

```bash
cd backend && ../.venv/bin/python -m pytest -q
```

All automated tests use synthetic fixtures and mocked providers; none touch the network. The live checks are listed
under Benchmark.
