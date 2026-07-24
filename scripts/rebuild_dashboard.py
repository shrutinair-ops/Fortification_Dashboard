#!/usr/bin/env python3
"""
=============================================================================
rebuild_dashboard.py
=============================================================================
PURPOSE:
  Fetches the latest data from the Google Sheets database tab via an Apps
  Script Web App, processes all metrics and calculations, and writes the
  result to data.json in the repository root.

  This script is run automatically every day at 10:00 AM IST by GitHub
  Actions (see .github/workflows/refresh-dashboard.yml).

  IMPORTANT: This script ONLY writes data.json.
             index.html is NEVER modified by this script.

DATA FLOW:
  Google Sheet (database tab)
    → Apps Script Web App (serves rows as JSON)
    → This script (processes + calculates)
    → data.json (stored in GitHub repo)
    → Browser (fetches data.json on page load, renders dashboard)

HOW TO RUN MANUALLY (for testing):
  export APPS_SCRIPT_URL="https://script.google.com/macros/s/YOUR_ID/exec"
  python scripts/rebuild_dashboard.py

DEPENDENCIES: pandas, numpy, pytz (installed by GitHub Actions workflow)
=============================================================================
"""

import os, sys, json, urllib.request
import pandas as pd
import numpy as np

# ── STEP 1: Get the Apps Script URL from environment variable ─────────────────
# The URL is stored as a GitHub Secret (repo Settings → Secrets → Actions).
# GitHub Actions injects it as an environment variable before running this script.
# It is NEVER hardcoded in the code to avoid exposing it publicly.
# If you run this locally, set the env var first (see HOW TO RUN MANUALLY above).
APPS_URL = os.environ.get('APPS_SCRIPT_URL', '')
if not APPS_URL:
    print("ERROR: APPS_SCRIPT_URL environment variable not set.")
    print("Add it as a GitHub Secret: repo Settings → Secrets → Actions")
    sys.exit(1)

# ── STEP 2: Fetch raw data from Apps Script ────────────────────────────────────
# The Apps Script is a Google Apps Script Web App deployed from the Google Sheet.
# When called with ?sheet=database&callback=__data__, it:
#   1. Opens the Google Sheet
#   2. Reads all rows from the 'database' tab
#   3. Returns them as JSONP: __data__({"sheet":"database","rows":[...]})
# We pass callback=__data__ so we can strip that wrapper and parse the JSON.
print("Fetching data from Apps Script...")
try:
    url = APPS_URL + "?sheet=database&callback=__data__"
    with urllib.request.urlopen(url, timeout=120) as r:
        raw = r.read().decode('utf-8')
    # Strip the JSONP wrapper: __data__({...}) → {...}
    data = json.loads(raw[len('__data__('):-1])
    rows = data['rows']   # List of dicts, one per row in the sheet
    print(f"Fetched {len(rows)} rows")
except Exception as e:
    print(f"ERROR fetching data: {e}")
    sys.exit(1)

# ── STEP 3: Build the main DataFrame ──────────────────────────────────────────
# Each row from the sheet becomes a row in the DataFrame.
# Column names match the sheet headers exactly (case-sensitive).
df = pd.DataFrame(rows)

# Convert month column from UTC ISO string to IST datetime, then to YYYY-MM string.
# The sheet stores dates as UTC end-of-month (e.g. "2026-05-31T18:30:00.000Z")
# which is actually 2026-06-01 00:00 IST. We convert to IST to get the right month.
df['Month_IST']       = pd.to_datetime(df['Month '], errors='coerce', utc=True).dt.tz_convert('Asia/Kolkata')
df['MonthStr']        = df['Month_IST'].dt.strftime('%Y-%m')   # e.g. "2026-05"

# Convert launch date to IST for launch age calculations
df['launch_date_IST'] = pd.to_datetime(df['Launch date'], errors='coerce', utc=True).dt.tz_convert('Asia/Kolkata')

# Convert numeric columns — sheet values come as strings, coerce errors to NaN
df['Production']      = pd.to_numeric(df['Fortified atta production volume (MT)'], errors='coerce')
df['Icheck']          = pd.to_numeric(df['Icheck result  (mg/kg)'], errors='coerce')
df['Beneficiaries']   = pd.to_numeric(df['Beneficiaries reached'], errors='coerce')
df['ProdDev']         = pd.to_numeric(df['% Deviation from 3-Month Avg'], errors='coerce')
df['Avg3M']           = pd.to_numeric(df['Avg Production Last 3 Months (MT)'], errors='coerce')

# ── Quality Officer: use SurveyCTO column first, fall back to original ────────
# SurveyCTO column is more accurate (filled by the field officer during the visit)
# If it's blank for a record, we use the manually entered Quality Officer column.
# We also normalize a known spelling variant so Subrajit appears as one entry.
df['qo_cto']    = df['Quality Officer from SurveyCTO'].str.strip().fillna('')
df['qo_orig']   = df['Quality Officer'].str.strip().fillna('')
df['qo_final']  = np.where(df['qo_cto'] != '', df['qo_cto'], df['qo_orig'])
df['qo_final']  = df['qo_final'].str.replace('Subrajit Majumder', 'Subrajit Mujumder', regex=False)

# Non-Monthly Visit flag: 1 = Non-Monthly, 0 = Monthly or not set
# 'NA' values (before Aug 2025 when program started) are treated as 0 (Monthly)
df['nm_flag']   = df['Selected for Non-Monthly Visit'].map({'Yes': 1, 'No': 0}).fillna(0).astype(int)

# Launch age in months: used to bucket mills into 0-2, 2-4, 4-6, 6-12, 12-24, 24+
# Calculated as (record month - launch date) in days, divided by avg days per month
df['launch_age_months'] = ((df['Month_IST'] - df['launch_date_IST']).dt.days / 30.44)

# Remove rows with no Mill Code (blank/header rows)
df = df[df['Mill Code'].notna() & (df['Mill Code'] != '')].copy()

# ── Launch age bucketing ───────────────────────────────────────────────────────
# Assigns each record to one of 6 granular age buckets.
# Finer granularity in the 0-6 month range because mills stabilise during this period
# and we want to see that stabilisation pattern in iCheck performance.
def age_bucket(a):
    if pd.isna(a) or a < 0: return 'Unknown'
    elif a <= 2:  return '0-2 months'
    elif a <= 4:  return '2-4 months'
    elif a <= 6:  return '4-6 months'
    elif a <= 12: return '6-12 months'
    elif a <= 24: return '12-24 months'
    else:         return '24+ months'
df['age_bucket'] = df['launch_age_months'].apply(age_bucket)

# ── RCA column definitions ────────────────────────────────────────────────────
# RCA = Root Cause Analysis. Each iCheck record can have multiple RCA flags (Yes/No).
# We store all 6 as a single integer using bit flags:
#   bit 0 (1)  = Human/Ops
#   bit 1 (2)  = Equipment
#   bit 2 (4)  = Microfeeder
#   bit 3 (8)  = Sampling
#   bit 4 (16) = Premix
#   bit 5 (32) = No Issue
# This compresses 6 Yes/No columns into 1 integer per record.
# In the dashboard JS, we use bitwise AND (r[F.RCA] & (1<<i)) to check each flag.
RCA_COLS = {
    'Human/Ops':   'RCA: Human / Operational / Protocol',
    'Equipment':   'RCA: Equipment / Mechanical / Electrical',
    'Microfeeder': 'RCA: Microfeeder Calibration / Set-up',
    'Sampling':    'RCA: Sampling Issue',
    'Premix':      'RCA: Premix Issue',
    'No Issue':    'RCA: No Issue / Retesting',
}
def rca_bits(row):
    b = 0
    for i, col in enumerate(RCA_COLS.values()):
        if row.get(col, '') == 'Yes':
            b |= (1 << i)   # Set the i-th bit
    return b

# ── iCheck score bucketing ────────────────────────────────────────────────────
# ICHS (iCheck Status) maps the raw mg/kg value to a category code:
#   0 = No iCheck record for this row
#   1 = Below range (< 14 mg/kg)
#   2 = Within range (14 – 21.25 mg/kg) ← acceptable range per programme standard
#   3 = Slightly above (21.26 – 28 mg/kg)
#   4 = Above range (> 28 mg/kg)
def ich_s(v):
    if pd.isna(v) or v == 0: return 0
    if v < 14:     return 1
    if v <= 21.25: return 2
    if v <= 28:    return 3
    return 4

# ── STEP 4: Determine data range ──────────────────────────────────────────────
# We use all data from programme start (July 2022) to the latest month in the sheet.
# This automatically includes new months as they appear in the sheet.
max_month = df['MonthStr'].max()
print(f"Data runs to: {max_month} (IST)")
base = df[(df['MonthStr'] >= '2022-07') & (df['MonthStr'] <= max_month)].copy()
print(f"Base records: {len(base)}")

# ── STEP 5: Pipeline counts ────────────────────────────────────────────────────
# Pipeline = how many mills are in each stage RIGHT NOW.
# Since the same mill appears across many months with potentially different stages,
# we take the LATEST record per mill (most recent month's stage).
latest_per_mill = df.sort_values('MonthStr').groupby('Mill Code').last().reset_index()
launched   = int((latest_per_mill['Mill Stage'] == 'Launched').sum())
pre_launch = int((latest_per_mill['Mill Stage'] == 'Pre-Launch').sum())
terminated = int((latest_per_mill['Mill Stage'] == 'Terminated').sum())
states_n   = int(latest_per_mill['State Name'].nunique())
clusters_n = int(latest_per_mill['Cluster Name'].nunique())

# Active mills = mills that had production in the last month with production data.
# We walk backwards from the latest month to find the last one with actual production
# (some months like July 2026 may have 0 production because data isn't reported yet).
prod_months = df[df['Production'] > 0].groupby('MonthStr').size()
last_prod_month = prod_months.index[-1] if len(prod_months) else max_month
active_mills = int(df[(df['MonthStr'] == last_prod_month) & (df['Production'] > 0)]['Mill Code'].nunique())
print(f"Pipeline: Launched={launched}, Pre-Launch={pre_launch}, Terminated={terminated}, Active={active_mills} (as of {last_prod_month})")

# ── STEP 6: Rolling 12-month production benchmark per mill ─────────────────────
# For each mill-month record, calculate the mean production of the
# PRIOR 12 months (excluding the current month). This is the "annual benchmark"
# used in the Production Action Flags table.
# Why 12 months? It averages across all seasons, so it's not distorted by
# seasonal highs/lows the way a 3-month average can be.
# Requires at least 3 prior months to be reliable (otherwise falls back to Avg3M).
print("Computing rolling 12-month benchmarks (this may take a moment)...")
base_sorted = base.sort_values(['Mill Code', 'MonthStr'])
ann_benchmark = {}   # Dict of (mill_code, month_str) → benchmark value
for mc, grp in base_sorted.groupby('Mill Code'):
    grp = grp.reset_index(drop=True)
    prods = grp[['MonthStr', 'Production']].copy()
    # prod_nz: production values but with 0 replaced by NaN (we exclude zero months)
    prods['prod_nz'] = prods['Production'].where(prods['Production'] > 0)
    for i, row in prods.iterrows():
        # Get the 12 months BEFORE this record's month
        prior = prods[prods['MonthStr'] < row['MonthStr']].tail(12)
        valid = prior['prod_nz'].dropna()   # Drop zero/null months
        # Only compute if we have at least 3 valid prior months
        ann_benchmark[(mc, row['MonthStr'])] = round(valid.mean(), 1) if len(valid) >= 3 else None

# ── STEP 7: Build lookup tables (for compact data encoding) ───────────────────
# Instead of storing "Madhya Pradesh" in every record, we store integer index 7.
# This makes data.json significantly smaller (about 30% reduction in file size).
# The dashboard JS reverses these lookups at render time.
states      = sorted(base['State Name'].dropna().unique().tolist())
clusters    = sorted(base['Cluster Name'].dropna().unique().tolist())
mills       = sorted(base['Mill Code'].dropna().unique().tolist())
mill_names  = dict(zip(base['Mill Code'], base['Mill Name']))   # mill_code → mill_name
qos         = sorted(base['qo_final'][base['qo_final'] != ''].unique().tolist())
pos_list    = sorted([p for p in base['Program Officer'].str.strip().fillna('').unique() if p])
cap_cats    = ['Below 100 MT/Month','100-300 MT/Month','300-1000 MT/Month','More than 1000 MT/ Month']
months_list = sorted(base['MonthStr'].unique().tolist())

# Launch months: all unique months in which a mill was launched (for the "Launched By" filter)
lm_raw = df['launch_date_IST'].dt.strftime('%Y-%m').dropna()
all_launch_months = sorted([m for m in lm_raw.unique() if m >= '2019-01'])

# Build mill → launch month mapping (each mill's first recorded launch date)
mill_launch = {}
for mc, grp in df.groupby('Mill Code'):
    lm = grp['launch_date_IST'].dt.strftime('%Y-%m').dropna()
    if len(lm): mill_launch[mc] = lm.iloc[0]

# Index maps: value → integer index (reverse of the lists above)
state_idx        = {s: i for i, s in enumerate(states)}
cluster_idx      = {c: i for i, c in enumerate(clusters)}
mill_idx         = {m: i for i, m in enumerate(mills)}
qo_idx           = {q: i for i, q in enumerate(qos)}
po_idx           = {p: i for i, p in enumerate(pos_list)}
cap_idx          = {c: i for i, c in enumerate(cap_cats)}
month_idx        = {m: i for i, m in enumerate(months_list)}
age_idx_map      = {'0-2 months':0,'2-4 months':1,'4-6 months':2,
                    '6-12 months':3,'12-24 months':4,'24+ months':5,'Unknown':6}
launch_month_idx = {m: i for i, m in enumerate(all_launch_months)}

# ── STEP 8: Build the records array ──────────────────────────────────────────
# Each record is a compact 18-field array (not a dict) for space efficiency.
# Field definitions (matches const F={...} in index.html):
#   [0]  MONTH    : index into months_list array
#   [1]  MILL     : index into mills array
#   [2]  STATE    : index into states array (-1 if unknown)
#   [3]  CLUSTER  : index into clusters array (-1 if unknown)
#   [4]  CAP      : index into cap_cats array (-1 if unknown)
#   [5]  AGE      : launch age bucket index (0-6)
#   [6]  ICHECK   : raw iCheck value in mg/kg (null if no iCheck this month)
#   [7]  ICHS     : iCheck status code (0=none, 1=below, 2=within, 3=slight above, 4=above)
#   [8]  PROD     : production in MT (null if 0 or no production)
#   [9]  PDEV     : % deviation from 3-month avg (from sheet column, null if missing)
#   [10] NM       : Non-Monthly flag (1=NM, 0=Monthly)
#   [11] RCA      : RCA bit flags (integer combining all 6 RCA columns)
#   [12] BENE     : beneficiaries reached (null if 0)
#   [13] AVG3M    : 3-month average production from sheet column (null if 0)
#   [14] QO       : index into qos array (-1 if unknown)
#   [15] PO       : index into pos_list array (-1 if unknown)
#   [16] LAUNCH_M : index into all_launch_months array (-1 if unknown)
#   [17] ANN_BM   : rolling 12-month production benchmark (null if insufficient data)
print("Building records array...")
records = []
for _, row in base.iterrows():
    mc = row['Mill Code']; ms = row['MonthStr']
    if mc not in mill_idx or ms not in month_idx: continue

    # Null-encode zero/missing values to save space in JSON
    ich_v = None if (pd.isna(row['Icheck']) or row['Icheck'] == 0) else round(float(row['Icheck']), 2)
    prod  = None if (pd.isna(row['Production']) or row['Production'] == 0) else round(float(row['Production']), 1)
    pdev  = None if pd.isna(row['ProdDev']) else round(float(row['ProdDev']), 1)
    bene  = None if (pd.isna(row['Beneficiaries']) or row['Beneficiaries'] == 0) else float(row['Beneficiaries'])
    avg3m = None if (pd.isna(row['Avg3M']) or row['Avg3M'] == 0) else round(float(row['Avg3M']), 1)
    ann_bm = ann_benchmark.get((mc, ms))   # None if not enough prior data
    qo = row['qo_final']
    po = row['Program Officer'].strip() if pd.notna(row['Program Officer']) else ''
    lm = mill_launch.get(mc)
    lm_idx = launch_month_idx.get(lm, -1) if lm else -1

    records.append([
        month_idx[ms], mill_idx[mc],
        state_idx.get(row['State Name'], -1),
        cluster_idx.get(row['Cluster Name'], -1),
        cap_idx.get(row['Mill Capacity Category'], -1),
        age_idx_map.get(row['age_bucket'], 6),
        ich_v, ich_s(row['Icheck']),
        prod, pdev, int(row['nm_flag']),
        rca_bits(dict(row)),
        bene, avg3m,
        qo_idx.get(qo, -1), po_idx.get(po, -1),
        lm_idx, ann_bm
    ])

# ── STEP 9: Build the EMBEDDED object ────────────────────────────────────────
# This is the main data object that the dashboard reads.
# Stored under the "EMBEDDED" key in data.json.
# The dashboard JS accesses it as: data.EMBEDDED
embedded = {
    # Lookup arrays (indices in records map back to these)
    'months':        months_list,       # All YYYY-MM strings in data range
    'states':        states,            # All unique state names
    'clusters':      clusters,          # All unique cluster names
    'mills':         mills,             # All unique mill codes
    'mill_names':    mill_names,        # mill_code → mill_name dict
    'cap_cats':      cap_cats,          # 4 capacity category labels
    'rca_labels':    list(RCA_COLS.keys()),  # 6 RCA factor labels
    'qos':           qos,               # All unique quality officer names
    'pos':           pos_list,          # All unique program officer names
    'launch_months': all_launch_months, # All unique launch months (for Launched By filter)
    'age_labels':    ['0-2 months','2-4 months','4-6 months',
                      '6-12 months','12-24 months','24+ months'],
    # Pipeline summary (shown in top tiles of Program Overview)
    'pipeline': {
        'launched':   launched,
        'pre_launch': pre_launch,
        'terminated': terminated,
        'active':     active_mills,   # Mills with production in last reporting month
        'states':     states_n,
        'clusters':   clusters_n
    },
    # The main data array — one 18-element list per mill-month record
    'records': records,
}
print(f"EMBEDDED: {len(records)} records, {len(all_launch_months)} launch months")

# ── STEP 10: Build ALL_FLAGS ───────────────────────────────────────────────────
# ALL_FLAGS is used by the Production Action Flags table (Tab 1 and Tab 2).
# One entry per mill, independent of the date filter in the dashboard.
# Stores: latest production trend, latest iCheck status, OOR streak count.
#
# OOR streak: how many consecutive recent months has this mill been out of range?
# (e.g. if last 3 months are OOR/OOR/In-Range, streak = 2)
all_m = months_list
last3 = all_m[-3:]   # Last 3 months in dataset (for "current" production window)
prev3 = all_m[-6:-3] # 3 months before that (for "prior" production window)

# Calculate OOR streak per mill
mill_ich = base[base['Icheck'].notna() & (base['Icheck'] != 0)].copy()
mill_ich['oor'] = ~((mill_ich['Icheck'] >= 14) & (mill_ich['Icheck'] <= 21.25))
persist = {}
for mc, grp in mill_ich.groupby('Mill Code'):
    sg = grp.sort_values('MonthStr', ascending=False)  # Latest first
    streak = 0
    for _, r in sg.iterrows():
        if r['oor']: streak += 1
        else: break   # Stop at first in-range month
    persist[mc] = streak

prod_flags = []
for mc, grp in base.groupby('Mill Code'):
    # Production trend: compare last 3 months avg vs prior 3 months avg
    cur = grp[grp['MonthStr'].isin(last3)]['Production'].replace(0, np.nan).dropna()
    prv = grp[grp['MonthStr'].isin(prev3)]['Production'].replace(0, np.nan).dropna()
    if len(cur) >= 1 and len(prv) >= 1:
        ca, pa = cur.mean(), prv.mean()
        pct = round((ca - pa) / pa * 100, 1) if pa > 0 else 0
        pstatus = 'Declining' if pct <= -20 else ('Improving' if pct >= 30 else 'Stable')
    elif len(cur) >= 1:
        ca = cur.mean(); pa = None; pct = None; pstatus = 'New'
    else:
        ca = None; pa = None; pct = None; pstatus = 'No Data'

    # Latest iCheck value and status
    ich_d = grp[grp['Icheck'].notna() & (grp['Icheck'] != 0)].sort_values('MonthStr', ascending=False)
    if len(ich_d):
        iv = ich_d.iloc[0]['Icheck']; im = ich_d.iloc[0]['MonthStr']
        ist = 'Below Range' if iv < 14 else ('Within Range' if iv <= 21.25 else ('21.26-28' if iv <= 28 else 'Above 28'))
    else:
        iv = im = None; ist = 'No Data'

    prod_flags.append({
        'mc': mc, 'name': grp['Mill Name'].iloc[0],
        'state': grp['State Name'].iloc[0], 'cluster': grp['Cluster Name'].iloc[0],
        'qo': str(grp['qo_final'].iloc[-1]),
        'po': str(grp['Program Officer'].iloc[-1]) if pd.notna(grp['Program Officer'].iloc[-1]) else '',
        'cur_avg': round(ca, 1) if ca else None, 'prv_avg': round(pa, 1) if pa else None,
        'pct': pct, 'pstatus': pstatus,
        'ich_val': round(iv, 2) if iv else None, 'ich_month': im,
        'ich_streak': persist.get(mc, 0), 'ichstatus': ist
    })
print(f"ALL_FLAGS: {len(prod_flags)} mills")

# ── STEP 11: Build REVERSIONS ────────────────────────────────────────────────
# A "reversion" is when a mill goes from Non-Monthly visit back to Monthly.
# We detect this by scanning each mill's visit status chronologically.
# For each reversion, we look at the 3 months before the reversion month
# to infer what triggered it (OOR iCheck, production decline, or production surge).
reversions = []
for mc, grp in base.groupby('Mill Code'):
    sg = grp.sort_values('MonthStr').reset_index(drop=True)
    was_nm = False; nm_start = None
    for i, row in sg.iterrows():
        nm = row['Selected for Non-Monthly Visit'] == 'Yes'
        if nm and not was_nm:
            # Mill just entered Non-Monthly status
            was_nm = True; nm_start = row['MonthStr']
        elif not nm and was_nm:
            # Mill just reverted from Non-Monthly to Monthly — record this event
            rm = row['MonthStr']
            pre = sg[sg['MonthStr'] < rm].tail(3)   # 3 months before reversion
            pre_ich = pre[pre['Icheck'].notna() & (pre['Icheck'] != 0)]['Icheck'].tolist()
            pre_s   = ['Below' if v < 14 else 'In Range' if v <= 21.25 else 'Above' for v in pre_ich]
            pre_pd  = pre[pre['ProdDev'].notna()]['ProdDev'].tolist()

            # Infer trigger: check what was happening in the 3 pre-reversion months
            reasons = []
            oor_c = sum(1 for s in pre_s if s != 'In Range')
            if oor_c >= 1: reasons.append(f"iCheck OOR ({oor_c} of {len(pre_s)} months)")
            if pre_pd:
                avg_dev = np.mean(pre_pd)
                if avg_dev < -20:  reasons.append(f"Production declining (avg {avg_dev:.1f}%)")
                elif avg_dev > 30: reasons.append(f"Production surge (avg {avg_dev:.1f}%)")
            if not reasons: reasons.append("No clear signal")

            nm_m = sg[(sg['MonthStr'] >= nm_start) & (sg['MonthStr'] < rm)]['MonthStr'].nunique()
            reversions.append({
                'mc': mc, 'name': sg['Mill Name'].iloc[0],
                'state': sg['State Name'].iloc[0], 'cluster': sg['Cluster Name'].iloc[0],
                'qo': str(sg['qo_final'].iloc[-1]),
                'po': str(sg['Program Officer'].iloc[-1]) if pd.notna(sg['Program Officer'].iloc[-1]) else '',
                'nm_start': nm_start, 'revert_month': rm, 'nm_duration': nm_m,
                'pre_ich': [round(v, 2) for v in pre_ich], 'pre_ich_status': pre_s,
                'pre_pdev': [round(v, 1) for v in pre_pd], 'trigger': '; '.join(reasons)
            })
            was_nm = False; nm_start = None
print(f"REVERSIONS: {len(reversions)} events")

# ── STEP 12: Write data.json ──────────────────────────────────────────────────
# data.json contains three top-level keys:
#   EMBEDDED   : main data (records, lookup arrays, pipeline counts)
#   REVERSIONS : list of NM→Monthly reversion events
#   ALL_FLAGS  : per-mill production and iCheck summary (for flags tables)
#
# The dashboard's index.html fetches this file on page load via:
#   fetch('data.json').then(data => { EMBEDDED=data.EMBEDDED; ... })
#
# NOTE: data.json is a plain JSON file — no HTML escaping needed.
# (The old architecture embedded data inside <script> tags in HTML,
# which required escaping </ as <\/. That is no longer needed here.)
output = {
    'EMBEDDED':   embedded,
    'REVERSIONS': reversions,
    'ALL_FLAGS':  prod_flags,
}
with open('data.json', 'w') as f:
    json.dump(output, f, separators=(',', ':'))   # separators=(',',':') removes whitespace = smaller file

import os as _os
size_kb = _os.path.getsize('data.json') // 1024
print(f"\n✅ data.json written successfully!")
print(f"   Size: {size_kb}KB")
print(f"   Data current as of: {max_month} (IST)")
print(f"   Records: {len(records)} | Mills: {len(mills)} | iCheck records: {sum(1 for r in records if r[6] is not None)}")
