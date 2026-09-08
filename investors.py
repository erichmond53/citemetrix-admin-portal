"""
Investor Intelligence — research pipeline for the admin portal.

Ported from the WordPress class-citemetrix-investors.php. Mirrors competition.py:
Perplexity deep research -> Claude fit-scoring -> upsert into wp_citemetrix_investors.
Plus suggest_investors(): mine wp_citemetrix_competition funding data for VC leads.

Reuses competition.py's key reader + model constants + requests plumbing.
"""
import json
import re
import requests

from competition import get_api_keys, PERPLEXITY_MODEL, CLAUDE_MODEL

FIT_LABELS = ('hot', 'warm', 'possible', 'cold')


# ─────────────────────────────────────────────────────────────────────────────
# Perplexity: deep VC research
# ─────────────────────────────────────────────────────────────────────────────
def perplexity_research(api_key, firm_name, website):
    subject = firm_name or website
    prompt = f"""Research the venture capital / investment firm "{subject}" thoroughly. I need detailed intelligence to evaluate fit for a fundraising pitch from CiteMetrix, a B2B SaaS AI Search Visibility platform.

Return a comprehensive research report covering:

1. FIRM OVERVIEW: Full name, website, HQ location, year founded, AUM if known, firm description/thesis in their own words.
2. INVESTMENT FOCUS: Industries, sectors, and technologies they prioritize. Specific keywords from their investment thesis (e.g. "AI/ML", "B2B SaaS", "martech", "SEO tech", "enterprise software", "developer tools").
3. STAGE & CHECK SIZE: What stages do they invest in (pre-seed, seed, Series A, B, C)? Typical check size or range.
4. RELEVANT PORTFOLIO: List portfolio companies similar to an AI Search Visibility / B2B SaaS / martech / SEO analytics platform. For each, explain briefly why they are relevant.
5. KEY PARTNERS: Names of partners who focus on AI, SaaS, marketing tech, or analytics — the right people to pitch.
6. RECENT ACTIVITY: Any investments in AI, SEO, content marketing, or martech in the last 12-18 months.
7. CONTACT & OUTREACH: Website pitch URL, contact email if public, LinkedIn firm page, any "submit a pitch" instructions.
8. RED FLAGS OR NOTES: Anything that might make this firm a poor fit.

Be specific and factual. Use real data where available."""

    try:
        response = requests.post(
            'https://api.perplexity.ai/chat/completions',
            headers={'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json'},
            json={
                'model': PERPLEXITY_MODEL,
                'messages': [
                    {'role': 'system', 'content': 'You are an expert venture capital researcher. Provide detailed, factual intelligence about investment firms.'},
                    {'role': 'user', 'content': prompt},
                ],
                'max_tokens': 2000,
            },
            timeout=90,
        )
    except Exception as e:
        return {'error': f'Perplexity request failed: {e}'}

    if response.status_code != 200:
        try:
            msg = response.json().get('error', {}).get('message', f'HTTP {response.status_code}')
        except Exception:
            msg = f'Perplexity HTTP {response.status_code}'
        return {'error': f'Perplexity error: {msg}'}

    return {'content': response.json()['choices'][0]['message']['content']}


# ─────────────────────────────────────────────────────────────────────────────
# Claude: structured fit analysis
# ─────────────────────────────────────────────────────────────────────────────
def claude_analyze(api_key, raw_research, firm_name, website):
    prompt = """You are a startup fundraising analyst. Below is a research report on a venture capital firm.
CiteMetrix is a B2B SaaS AI Search Visibility platform — we help brands track and improve how they appear in AI chatbot responses (ChatGPT, Claude, Perplexity, Gemini, etc.).
Current stage: early-stage, seeking seed to Series A investment. Recurring SaaS revenue, SMB and agency market, strong AI/tech angle.

Analyze the research and return ONLY a valid JSON object with this exact structure (no markdown, no preamble):

{
  "firm_name": "Full official firm name",
  "website": "canonical website domain",
  "hq": "City, State/Country",
  "stage_focus": "e.g. Seed, Series A",
  "check_size": "e.g. $500K-$2M or Unknown",
  "sectors": ["short sector tags this firm invests in, e.g. AI/ML", "SaaS", "B2B", "Fintech", "DevTools", "MarTech", "Healthtech"],
  "thesis": "1-2 sentence summary of their investment thesis in their own words",
  "fit_score": 0-100,
  "fit_label": "hot|warm|possible|cold",
  "fit_rationale": "2-3 sentences explaining specifically why this firm is or isn't a strong fit for CiteMetrix right now",
  "key_partners": ["Partner Name (focus area)", "..."],
  "portfolio": [
    {"company": "Company Name", "why": "1 sentence on why this portfolio company signals fit for CiteMetrix"}
  ],
  "contact": {
    "pitch_url": "https://...",
    "email": "pitch@firm.com or empty string",
    "linkedin": "https://linkedin.com/company/... or empty string",
    "notes": "any specific outreach instructions"
  },
  "red_flags": "Any reasons this firm might be a poor fit, or empty string"
}

Fit scoring guide:
- 80-100 (hot): Invests in our exact stage, clear AI/SaaS/martech thesis, relevant portfolio, right check size
- 60-79 (warm): Good stage/sector match but 1-2 gaps
- 40-59 (possible): Some overlap but meaningful mismatches
- 0-39 (cold): Poor fit — wrong stage, wrong sector, or closed to new investments

RESEARCH REPORT:
""" + (raw_research or '')

    try:
        response = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={'x-api-key': api_key, 'anthropic-version': '2023-06-01', 'Content-Type': 'application/json'},
            json={
                'model': CLAUDE_MODEL,
                'max_tokens': 2000,
                'messages': [{'role': 'user', 'content': prompt}],
            },
            timeout=90,
        )
    except Exception as e:
        return {'error': f'Claude request failed: {e}'}

    if response.status_code != 200:
        try:
            msg = response.json().get('error', {}).get('message', f'HTTP {response.status_code}')
        except Exception:
            msg = f'Claude HTTP {response.status_code}'
        return {'error': f'Claude error: {msg}'}

    text = response.json().get('content', [{}])[0].get('text', '')
    text = re.sub(r'^```(?:json)?\s*', '', text.strip())
    text = re.sub(r'\s*```$', '', text)
    text = re.sub(r'[\x00-\x1f]', ' ', text)  # strip control chars (same guard as the batch pipeline)
    try:
        parsed = json.loads(text)
    except Exception as e:
        return {'error': f'Failed to parse Claude JSON: {e}'}
    if not isinstance(parsed, dict) or not parsed.get('firm_name'):
        return {'error': 'Claude returned no firm_name'}
    return parsed


# ─────────────────────────────────────────────────────────────────────────────
# Upsert into wp_citemetrix_investors (by firm_name)
# ─────────────────────────────────────────────────────────────────────────────
def upsert_investor(db_conn, analysis, raw_research):
    fit_label = analysis.get('fit_label') if analysis.get('fit_label') in FIT_LABELS else 'cold'
    row = {
        'firm_name': (analysis.get('firm_name') or '')[:255],
        'website': (analysis.get('website') or '')[:255],
        'hq': (analysis.get('hq') or '')[:255],
        'stage_focus': (analysis.get('stage_focus') or '')[:255],
        'check_size': (analysis.get('check_size') or '')[:100],
        'thesis': analysis.get('thesis') or '',
        'sectors_json': json.dumps(analysis.get('sectors') or []),
        'fit_score': min(100, max(0, int(analysis.get('fit_score') or 0))),
        'fit_label': fit_label,
        'fit_rationale': analysis.get('fit_rationale') or '',
        'key_partners': json.dumps(analysis.get('key_partners') or []),
        'portfolio_json': json.dumps(analysis.get('portfolio') or []),
        'contact_json': json.dumps(analysis.get('contact') or {}),
        'raw_research': raw_research,
        'status': 'active',
        'researched_at': __import__('datetime').datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
    }
    cur = db_conn.cursor()
    cur.execute("SELECT id FROM wp_citemetrix_investors WHERE firm_name = %s LIMIT 1", (row['firm_name'],))
    existing = cur.fetchone()
    if existing:
        inv_id = existing['id'] if isinstance(existing, dict) else existing[0]
        sets = ', '.join(f"{k} = %s" for k in row)
        cur.execute(f"UPDATE wp_citemetrix_investors SET {sets} WHERE id = %s", list(row.values()) + [inv_id])
        db_conn.commit()
        return int(inv_id)
    cols = ', '.join(row.keys())
    ph = ', '.join(['%s'] * len(row))
    cur.execute(f"INSERT INTO wp_citemetrix_investors ({cols}) VALUES ({ph})", list(row.values()))
    db_conn.commit()
    return int(cur.lastrowid)


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration (background-job entry points, mirror competition.py)
# ─────────────────────────────────────────────────────────────────────────────
def research_investor(db_conn, admin_cursor, firm_name, website, progress=None):
    keys = get_api_keys(admin_cursor)
    if not keys.get('perplexity'):
        return {'error': 'Perplexity API key not configured. Add it in Settings.'}
    if not keys.get('anthropic'):
        return {'error': 'Anthropic API key not configured. Add it in Settings.'}

    if progress:
        progress('Researching the firm across the web...')
    research = perplexity_research(keys['perplexity'], firm_name, website)
    if 'error' in research:
        return research

    if progress:
        progress('Scoring fit with Claude...')
    analysis = claude_analyze(keys['anthropic'], research['content'], firm_name, website)
    if 'error' in analysis:
        return analysis

    if progress:
        progress('Saving results...')
    inv_id = upsert_investor(db_conn, analysis, research['content'])
    return {'success': True, 'investor_id': inv_id,
            'firm_name': analysis.get('firm_name'), 'fit_label': analysis.get('fit_label'),
            'fit_score': analysis.get('fit_score')}


def rescan_investor(db_conn, admin_cursor, investor_id, progress=None):
    cur = db_conn.cursor()
    cur.execute("SELECT firm_name, website FROM wp_citemetrix_investors WHERE id = %s", (investor_id,))
    row = cur.fetchone()
    if not row:
        return {'error': 'Investor not found.'}
    return research_investor(db_conn, admin_cursor, row['firm_name'], row['website'], progress=progress)


def suggest_investors(db_conn, admin_cursor, progress=None):
    """Mine wp_citemetrix_competition funding data -> Claude -> investor-firm leads."""
    keys = get_api_keys(admin_cursor)
    cur = db_conn.cursor()
    cur.execute("""SELECT company_name, funding_total, funding_stage, raw_research
                   FROM wp_citemetrix_competition
                   WHERE status = 'active' AND raw_research IS NOT NULL AND raw_research != ''
                   ORDER BY competitive_score DESC""")
    competitors = cur.fetchall()
    if not competitors:
        return {'suggestions': []}

    cur.execute("SELECT LOWER(firm_name) AS fn FROM wp_citemetrix_investors WHERE status = 'active'")
    tracked = {r['fn'] for r in cur.fetchall()}

    pattern = re.compile(
        r'[^.!?\n]*(?:fund(?:ed|ing|s)?|investor|invest(?:ed|ment|ors)?|backed|raised|venture|capital|series\s+[a-z]|seed\s+round|led\s+by|portfolio|angel)[^.!?\n]*',
        re.IGNORECASE)
    blocks = []
    for c in competitors:
        name = c['company_name']
        funding = (f"{c.get('funding_total') or ''} {c.get('funding_stage') or ''}").strip()
        raw = c.get('raw_research') or ''
        sentences = [s.strip() for s in pattern.findall(raw) if s.strip()]
        funding_text = '. '.join(sentences[:30])[:2500]
        if not funding_text:
            funding_text = raw[:1000]
        blocks.append(f"Company: {name}" + (f" | Known funding: {funding}" if funding else "") + f"\n{funding_text}")
    context = '\n\n---\n\n'.join(blocks)

    if progress:
        progress('Extracting investor firms from competitor funding...')
    if not keys.get('anthropic'):
        # Fallback: raw funding strings as unverified suggestions
        out = [{'firm_name': c['funding_total'], 'sources': [c['company_name']],
                'note': 'From funding record — verify firm name'}
               for c in competitors if c.get('funding_total')]
        return {'suggestions': out[:10]}

    prompt = """Below is funding and investor research data scraped from competitor profiles of companies in the AI search visibility / B2B SaaS / martech space.

Extract every named venture capital firm, investment fund, or notable angel investor mentioned. Return ONLY a JSON array with this structure — no markdown, no preamble:

[
  {"firm_name": "Accel", "source_companies": ["Otterly AI", "BrightEdge"]}
]

Rules:
- Include only real, named investment firms or well-known angels — not generic terms like "angel investors" or "bootstrapped"
- Deduplicate: if a firm appears in multiple companies, list all source companies
- Ignore terms like "undisclosed", "self-funded", "friends and family"
- Return at most 30 results, prioritized by how many source companies share the investor

COMPETITOR FUNDING DATA:
""" + context

    try:
        response = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={'x-api-key': keys['anthropic'], 'anthropic-version': '2023-06-01', 'Content-Type': 'application/json'},
            json={'model': CLAUDE_MODEL, 'max_tokens': 2000, 'messages': [{'role': 'user', 'content': prompt}]},
            timeout=60,
        )
    except Exception:
        return {'suggestions': []}
    if response.status_code != 200:
        return {'suggestions': []}

    text = response.json().get('content', [{}])[0].get('text', '')
    text = re.sub(r'^```(?:json)?\s*', '', text.strip())
    text = re.sub(r'\s*```$', '', text)
    text = re.sub(r'[\x00-\x1f]', ' ', text)
    try:
        parsed = json.loads(text)
    except Exception:
        return {'suggestions': []}
    if not isinstance(parsed, list):
        return {'suggestions': []}

    out = []
    for item in parsed:
        if not isinstance(item, dict) or not item.get('firm_name'):
            continue
        if item['firm_name'].lower() in tracked:
            continue
        out.append({'firm_name': item['firm_name'],
                    'sources': list(item.get('source_companies') or [])})
    return {'suggestions': out[:20]}
