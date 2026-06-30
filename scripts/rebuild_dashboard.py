#!/usr/bin/env python3
"""
rebuild_dashboard.py  (data.json architecture)
Fetches latest data from Google Apps Script and writes a fresh data.json.
index.html is a static shell and is NEVER modified by this script.
Run daily by GitHub Actions.
"""

import os, sys, json, urllib.request
import pandas as pd
import numpy as np

APPS_URL = os.environ.get('APPS_SCRIPT_URL', '')
if not APPS_URL:
    print("ERROR: APPS_SCRIPT_URL environment variable not set")
    sys.exit(1)

print("Fetching data from Apps Script...")
try:
    url = APPS_URL + "?sheet=database&callback=__data__"
    with urllib.request.urlopen(url, timeout=120) as r:
        raw = r.read().decode('utf-8')
    data = json.loads(raw[len('__data__('):-1])
    rows = data['rows']
    print(f"Fetched {len(rows)} rows")
except Exception as e:
    print(f"ERROR fetching data: {e}")
    sys.exit(1)

# ── Build dataframe ────────────────────────────────────────────────────────────
df = pd.DataFrame(rows)
df['Month_IST']       = pd.to_datetime(df['Month '], errors='coerce', utc=True).dt.tz_convert('Asia/Kolkata')
df['MonthStr']        = df['Month_IST'].dt.strftime('%Y-%m')
df['launch_date_IST'] = pd.to_datetime(df['Launch date'], errors='coerce', utc=True).dt.tz_convert('Asia/Kolkata')
df['Production']      = pd.to_numeric(df['Fortified atta production volume (MT)'], errors='coerce')
df['Icheck']          = pd.to_numeric(df['Icheck result  (mg/kg)'], errors='coerce')
df['Beneficiaries']   = pd.to_numeric(df['Beneficiaries reached'], errors='coerce')
df['ProdDev']         = pd.to_numeric(df['% Deviation from 3-Month Avg'], errors='coerce')
df['Avg3M']           = pd.to_numeric(df['Avg Production Last 3 Months (MT)'], errors='coerce')

df['qo_cto']    = df['Quality Officer from SurveyCTO'].str.strip().fillna('')
df['qo_orig']   = df['Quality Officer'].str.strip().fillna('')
df['qo_final']  = np.where(df['qo_cto'] != '', df['qo_cto'], df['qo_orig'])
df['qo_final']  = df['qo_final'].str.replace('Subrajit Majumder', 'Subrajit Mujumder', regex=False)

df['nm_flag']   = df['Selected for Non-Monthly Visit'].map({'Yes': 1, 'No': 0}).fillna(0).astype(int)
df['launch_age_months'] = ((df['Month_IST'] - df['launch_date_IST']).dt.days / 30.44)
df = df[df['Mill Code'].notna() & (df['Mill Code'] != '')].copy()

def age_bucket(a):
    if pd.isna(a) or a < 0: return 'Unknown'
    elif a <= 2:  return '0-2 months'
    elif a <= 4:  return '2-4 months'
    elif a <= 6:  return '4-6 months'
    elif a <= 12: return '6-12 months'
    elif a <= 24: return '12-24 months'
    else:         return '24+ months'
df['age_bucket'] = df['launch_age_months'].apply(age_bucket)

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
        if row.get(col, '') == 'Yes': b |= (1 << i)
    return b

def ich_s(v):
    if pd.isna(v) or v == 0: return 0
    if v < 14:     return 1
    if v <= 21.25: return 2
    if v <= 28:    return 3
    return 4

max_month = df['MonthStr'].max()
print(f"Data runs to: {max_month} (IST)")
base = df[(df['MonthStr'] >= '2022-07') & (df['MonthStr'] <= max_month)].copy()
print(f"Base records: {len(base)}")

# ── Pipeline counts ────────────────────────────────────────────────────────────
latest_per_mill = df.sort_values('MonthStr').groupby('Mill Code').last().reset_index()
launched   = int((latest_per_mill['Mill Stage'] == 'Launched').sum())
pre_launch = int((latest_per_mill['Mill Stage'] == 'Pre-Launch').sum())
terminated = int((latest_per_mill['Mill Stage'] == 'Terminated').sum())
states_n   = int(latest_per_mill['State Name'].nunique())
clusters_n = int(latest_per_mill['Cluster Name'].nunique())

prod_months = df[df['Production'] > 0].groupby('MonthStr').size()
last_prod_month = prod_months.index[-1] if len(prod_months) else max_month
active_mills = int(df[(df['MonthStr'] == last_prod_month) & (df['Production'] > 0)]['Mill Code'].nunique())
print(f"Pipeline: Launched={launched}, Pre-Launch={pre_launch}, Terminated={terminated}, Active={active_mills}")

# ── Rolling 12-month production benchmark per mill ─────────────────────────────
base_sorted = base.sort_values(['Mill Code', 'MonthStr'])
ann_benchmark = {}
for mc, grp in base_sorted.groupby('Mill Code'):
    grp = grp.reset_index(drop=True)
    prods = grp[['MonthStr', 'Production']].copy()
    prods['prod_nz'] = prods['Production'].where(prods['Production'] > 0)
    for i, row in prods.iterrows():
        prior = prods[prods['MonthStr'] < row['MonthStr']].tail(12)
        valid = prior['prod_nz'].dropna()
        ann_benchmark[(mc, row['MonthStr'])] = round(valid.mean(), 1) if len(valid) >= 3 else None

# ── Build lookup tables ────────────────────────────────────────────────────────
states      = sorted(base['State Name'].dropna().unique().tolist())
clusters    = sorted(base['Cluster Name'].dropna().unique().tolist())
mills       = sorted(base['Mill Code'].dropna().unique().tolist())
mill_names  = dict(zip(base['Mill Code'], base['Mill Name']))
qos         = sorted(base['qo_final'][base['qo_final'] != ''].unique().tolist())
pos_list    = sorted([p for p in base['Program Officer'].str.strip().fillna('').unique() if p])
cap_cats    = ['Below 100 MT/Month','100-300 MT/Month','300-1000 MT/Month','More than 1000 MT/ Month']
months_list = sorted(base['MonthStr'].unique().tolist())

lm_raw = df['launch_date_IST'].dt.strftime('%Y-%m').dropna()
all_launch_months = sorted([m for m in lm_raw.unique() if m >= '2019-01'])
mill_launch = {}
for mc, grp in df.groupby('Mill Code'):
    lm = grp['launch_date_IST'].dt.strftime('%Y-%m').dropna()
    if len(lm): mill_launch[mc] = lm.iloc[0]

state_idx   = {s: i for i, s in enumerate(states)}
cluster_idx = {c: i for i, c in enumerate(clusters)}
mill_idx    = {m: i for i, m in enumerate(mills)}
qo_idx      = {q: i for i, q in enumerate(qos)}
po_idx      = {p: i for i, p in enumerate(pos_list)}
cap_idx     = {c: i for i, c in enumerate(cap_cats)}
month_idx   = {m: i for i, m in enumerate(months_list)}
age_idx_map = {'0-2 months':0,'2-4 months':1,'4-6 months':2,'6-12 months':3,'12-24 months':4,'24+ months':5,'Unknown':6}
launch_month_idx = {m: i for i, m in enumerate(all_launch_months)}

# ── Build records (18 fields) ──────────────────────────────────────────────────
# [MONTH,MILL,STATE,CLUSTER,CAP,AGE,ICHECK,ICHS,PROD,PDEV,NM,RCA,BENE,AVG3M,QO,PO,LAUNCH_M,ANN_BM]
records = []
for _, row in base.iterrows():
    mc = row['Mill Code']; ms = row['MonthStr']
    if mc not in mill_idx or ms not in month_idx: continue
    ich_v = None if (pd.isna(row['Icheck']) or row['Icheck'] == 0) else round(float(row['Icheck']), 2)
    prod  = None if (pd.isna(row['Production']) or row['Production'] == 0) else round(float(row['Production']), 1)
    pdev  = None if pd.isna(row['ProdDev']) else round(float(row['ProdDev']), 1)
    bene  = None if (pd.isna(row['Beneficiaries']) or row['Beneficiaries'] == 0) else float(row['Beneficiaries'])
    avg3m = None if (pd.isna(row['Avg3M']) or row['Avg3M'] == 0) else round(float(row['Avg3M']), 1)
    ann_bm = ann_benchmark.get((mc, ms))
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

embedded = {
    'months': months_list, 'states': states, 'clusters': clusters,
    'mills': mills, 'mill_names': mill_names, 'cap_cats': cap_cats,
    'rca_labels': list(RCA_COLS.keys()), 'qos': qos, 'pos': pos_list,
    'launch_months': all_launch_months,
    'age_labels': ['0-2 months','2-4 months','4-6 months','6-12 months','12-24 months','24+ months'],
    'pipeline': {
        'launched': launched, 'pre_launch': pre_launch, 'terminated': terminated,
        'active': active_mills, 'states': states_n, 'clusters': clusters_n
    },
    'records': records,
}
print(f"EMBEDDED: {len(records)} records, {len(all_launch_months)} launch months")

# ── Build ALL_FLAGS ────────────────────────────────────────────────────────────
all_m = months_list
last3 = all_m[-3:]
prev3 = all_m[-6:-3]

mill_ich = base[base['Icheck'].notna() & (base['Icheck'] != 0)].copy()
mill_ich['oor'] = ~((mill_ich['Icheck'] >= 14) & (mill_ich['Icheck'] <= 21.25))
persist = {}
for mc, grp in mill_ich.groupby('Mill Code'):
    sg = grp.sort_values('MonthStr', ascending=False)
    streak = 0
    for _, r in sg.iterrows():
        if r['oor']: streak += 1
        else: break
    persist[mc] = streak

prod_flags = []
for mc, grp in base.groupby('Mill Code'):
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

# ── Build REVERSIONS ──────────────────────────────────────────────────────────
reversions = []
for mc, grp in base.groupby('Mill Code'):
    sg = grp.sort_values('MonthStr').reset_index(drop=True)
    was_nm = False; nm_start = None
    for i, row in sg.iterrows():
        nm = row['Selected for Non-Monthly Visit'] == 'Yes'
        if nm and not was_nm:
            was_nm = True; nm_start = row['MonthStr']
        elif not nm and was_nm:
            rm = row['MonthStr']
            pre = sg[sg['MonthStr'] < rm].tail(3)
            pre_ich = pre[pre['Icheck'].notna() & (pre['Icheck'] != 0)]['Icheck'].tolist()
            pre_s   = ['Below' if v < 14 else 'In Range' if v <= 21.25 else 'Above' for v in pre_ich]
            pre_pd  = pre[pre['ProdDev'].notna()]['ProdDev'].tolist()
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

# ── Write data.json (index.html is NEVER touched) ──────────────────────────────
output = {
    'EMBEDDED': embedded,
    'REVERSIONS': reversions,
    'ALL_FLAGS': prod_flags,
}
with open('data.json', 'w') as f:
    json.dump(output, f, separators=(',', ':'))

import os as _os
size_kb = _os.path.getsize('data.json') // 1024
print(f"\ndata.json written successfully! Size: {size_kb}KB")
print(f"Data current as of: {max_month} (IST)")
