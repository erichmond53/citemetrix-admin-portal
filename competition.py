
"""

CiteMetrix Competition Research Module

Handles Perplexity web research and Claude analysis for competitor intelligence.

"""

import os

import json

import re

import requests

from datetime import datetime



# Models

PERPLEXITY_MODEL = "sonar-pro"

CLAUDE_MODEL = "claude-sonnet-4-6"



def get_api_keys(cursor):

    """Retrieve API keys from settings table."""

    cursor.execute("SELECT setting_key, setting_value FROM settings WHERE setting_key IN ('anthropic_api_key', 'perplexity_api_key')")

    rows = cursor.fetchall()

    keys = {}

    for row in rows:

        if row['setting_key'] == 'anthropic_api_key':

            keys['anthropic'] = row['setting_value']

        elif row['setting_key'] == 'perplexity_api_key':

            keys['perplexity'] = row['setting_value']

    return keys



def get_citemetrix_context():

    """CiteMetrix platform description for competitive analysis.

    Delegates to citemetrix_inventory module — single source of truth.
    To update the context, edit citemetrix_inventory.py."""

    from citemetrix_inventory import get_citemetrix_context as _get_context

    return _get_context()





def fetch_website_content(url):

    """Fetch and extract readable content from a website as fallback."""

    url = url.rstrip('/')

    pages = {

        '': 'Homepage',

        '/about': 'About',

        '/pricing': 'Pricing',

        '/features': 'Features',

        '/product': 'Product',

        '/platform': 'Platform',

    }

    

    all_content = ''

    fetched = 0

    headers = {'User-Agent': 'Mozilla/5.0 (compatible; CiteMetrix/2.0; +https://citemetrix.com)'}

    

    for path, label in pages.items():

        if fetched >= 4:

            break

        

        page_url = url + path

        try:

            response = requests.get(page_url, headers=headers, timeout=15, allow_redirects=True)

            if response.status_code != 200:

                continue

            

            html = response.text

            if not html:

                continue

            

            # Strip scripts, styles, SVG, nav, footer, header tags

            html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)

            html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)

            html = re.sub(r'<svg[^>]*>.*?</svg>', '', html, flags=re.DOTALL | re.IGNORECASE)

            html = re.sub(r'<nav[^>]*>.*?</nav>', '', html, flags=re.DOTALL | re.IGNORECASE)

            html = re.sub(r'<footer[^>]*>.*?</footer>', '', html, flags=re.DOTALL | re.IGNORECASE)

            html = re.sub(r'<header[^>]*>.*?</header>', '', html, flags=re.DOTALL | re.IGNORECASE)

            html = re.sub(r'<noscript[^>]*>.*?</noscript>', '', html, flags=re.DOTALL | re.IGNORECASE)

            

            # Extract text

            text = re.sub(r'<[^>]+>', '', html)

            text = re.sub(r'[ \t]+', ' ', text)

            text = re.sub(r'\n{3,}', '\n\n', text)

            text = text.strip()

            

            if len(text) > 100:

                if len(text) > 3000:

                    text = text[:3000] + '... [truncated]'

                all_content += f"### {label} ({page_url})\n{text}\n\n"

                fetched += 1

        except Exception:

            continue

    

    if not all_content:

        return None

    

    if len(all_content) > 10000:

        all_content = all_content[:10000] + "\n\n... [content truncated]"

    

    return all_content





def perplexity_research(api_key, name, url):

    """Step 1: Perplexity web research on competitor."""

    domain_clean = re.sub(r'^https?://(www\.)?', '', url.rstrip('/')) if url else ''

    

    if not name and domain_clean:

        name = domain_clean

    

    prompt = f"IMPORTANT: Research ONLY the company at the exact website {url} (domain: {domain_clean})."

    if name and name != domain_clean:

        prompt += f' The company name is "{name}".'

    

    prompt += f"""



DO NOT research or return information about any other company, even if they have a similar name. If you cannot find information specifically about {domain_clean}, say so — do NOT substitute a different company's information.



Visit {url} and provide the following about THIS specific company:



1. **Company Overview**: Full name as shown on {domain_clean}, tagline/positioning, founded date, headquarters, estimated employee count

2. **Funding**: Total funding raised, last round details, key investors, valuation if known

3. **Product & Features**: What does their platform do? List ALL features in detail. What AI platforms do they monitor/support?

4. **Pricing**: All pricing tiers, what's included in each, any enterprise/custom pricing

5. **Target Market**: Who are their customers? Any named clients or case studies?

6. **Technology**: How does their platform work? BYOK model? API-based? Browser extension? Scraping?

7. **Recent News**: Any recent product launches, partnerships, hires, pivots, or announcements from the past 6 months

8. **Team**: Key leadership (CEO, CTO, notable hires)

9. **Strengths**: What are they doing well? What do users praise?

10. **Weaknesses**: What are users complaining about? What features are missing? What are their known limitations?



CRITICAL: All information must be about the company at {domain_clean}. Start your response by confirming the company name and URL you are researching. Be thorough and cite your sources. Include specific numbers wherever possible (pricing, user counts, platform counts, etc.)."""



    response = requests.post(

        'https://api.perplexity.ai/chat/completions',

        headers={

            'Authorization': f'Bearer {api_key}',

            'Content-Type': 'application/json',

        },

        json={

            'model': PERPLEXITY_MODEL,

            'messages': [

                {'role': 'system', 'content': 'You are a thorough competitive intelligence researcher. You MUST research only the specific company and website URL provided. Never substitute information from a different company. If you cannot find information about the specific company, clearly state that rather than providing information about a similarly-named company.'},

                {'role': 'user', 'content': prompt},

            ],

            'max_tokens': 4000,

            'temperature': 0.1,

        },

        timeout=60

    )

    

    if response.status_code != 200:

        error_msg = response.json().get('error', {}).get('message', f'Perplexity API returned HTTP {response.status_code}')

        return {'error': error_msg}

    

    data = response.json()

    return {'content': data['choices'][0]['message']['content']}





def claude_analyze(api_key, name, url, research_data):

    """Step 2: Claude analysis (SWOT, scoring, recommendations)."""

    citemetrix_context = get_citemetrix_context()

    domain_clean = re.sub(r'^https?://(www\.)?', '', url.rstrip('/')) if url else ''

    

    prompt = f"""You are a competitive intelligence analyst for CiteMetrix. Below is research data about a competitor: {name}""" + (f" ({url})" if url else "") + f""".



## CRITICAL: Company Verification

The research below should be about the company at {domain_clean}. BEFORE analyzing, verify the research actually describes this company. If the research describes a DIFFERENT company (wrong URL, wrong product, wrong industry), set "data_mismatch" to true in your JSON output and set "mismatch_reason" to explain what went wrong. Do NOT proceed with a full analysis of the wrong company.



NOTE: The research data may include direct website content scraped from {domain_clean}. If present, treat the scraped website content as the PRIMARY and most authoritative source. Any Perplexity research that contradicts the direct website content should be disregarded.



## Research Data

{research_data}



## About CiteMetrix (our platform)

{citemetrix_context}



## Your Task

Analyze this competitor against CiteMetrix and return a JSON object.



## CRITICAL: Feature Parity Rules



The most important part of this analysis is correctly classifying features into 'they_have_we_dont', 'we_have_they_dont', and 'both_have'. You MUST reason about FUNCTIONAL EQUIVALENCE, not literal name matching.



**How to evaluate feature parity:**

1. For each competitor feature, ask: "What underlying CAPABILITY does this provide to users?"

2. Then ask: "Does CiteMetrix provide this same capability, even if named differently or implemented differently?"

3. Only place a feature in 'they_have_we_dont' if CiteMetrix genuinely has NO way to accomplish the same outcome.



**Examples of functional equivalence:**

- Competitor has 'Traffic Attribution via CDN' → CiteMetrix has GA4 integration that provides traffic attribution → this is 'both_have'

- Competitor has 'AI Response Monitoring' → CiteMetrix has Citation Tracking and Monitored Prompts → this is 'both_have'

- Competitor has 'Sentiment Scoring' → CiteMetrix has Sentiment Analysis → this is 'both_have'

- Competitor has 'Custom Query Builder' → CiteMetrix has Monitored Prompts / Custom Prompt Builder → this is 'both_have'

- Competitor has 'Content Recommendations' → CiteMetrix has Content Auditor and remediation tools → this is 'both_have'

- Competitor has 'Search Console Data' → CiteMetrix has GSC integration via OAuth → this is 'both_have'

- Competitor has 'Proprietary LLM for analysis' → CiteMetrix does NOT have a proprietary LLM → this is genuinely 'they_have_we_dont'



**The 'they_have_we_dont' list should be SHORT and contain only truly novel capabilities CiteMetrix cannot replicate with its existing tools.** When in doubt, place the feature in 'both_have' with an honest 'who_does_better' assessment rather than incorrectly claiming CiteMetrix lacks the capability entirely.



## Output Format

Return ONLY a valid JSON object (no markdown backticks, no explanation, no preamble) with exactly this structure:



{{

    "company_name": "string",

    "company_url": "string (canonical URL)",

    "tagline": "string",

    "founded": "string (year or date)",

    "headquarters": "string",

    "employee_count": "string (estimate if unknown)",

    "funding_total": "string (e.g. '$5M' or 'Bootstrapped')",

    "funding_stage": "string (e.g. 'Seed', 'Series A', 'Bootstrapped')",

    "platforms_supported": ["list of AI platforms they monitor"],

    "pricing_summary": {{

        "tiers": [{{"name": "string", "price": "string", "features": ["list"]}}],

        "model": "string (subscription/usage/freemium/etc)"

    }},

    "key_features": [

        {{"feature": "string", "description": "string", "citemetrix_has": true/false}}

    ],

    "swot_analysis": {{

        "strengths": ["list of their strengths vs us"],

        "weaknesses": ["list of their weaknesses vs us"],

        "opportunities": ["market opportunities we can exploit against them"],

        "threats": ["threats they pose to CiteMetrix"]

    }},

    "competitive_score": integer 0-100,

    "threat_level": "critical|high|medium|low|watch",

    "strategy_recommendations": [

        {{"priority": "high|medium|low", "action": "string", "rationale": "string", "effort": "string (low/medium/high)"}}

    ],

    "feature_parity": {{

        "they_have_we_dont": [{{"feature": "string", "importance": "high|medium|low", "build_effort": "string"}}],

        "we_have_they_dont": [{{"feature": "string", "moat_strength": "strong|moderate|weak"}}],

        "both_have": [{{"feature": "string", "who_does_better": "citemetrix|them|comparable"}}]

    }},

    "market_position": "string (1-2 sentence summary)",

    "data_mismatch": false,

    "mismatch_reason": null

}}



IMPORTANT: Return ONLY the JSON object. No markdown backticks, no explanation, no preamble."""



    response = requests.post(

        'https://api.anthropic.com/v1/messages',

        headers={

            'x-api-key': api_key,

            'anthropic-version': '2023-06-01',

            'Content-Type': 'application/json',

        },

        json={

            'model': CLAUDE_MODEL,

            'max_tokens': 8000,

            'temperature': 0.2,

            'messages': [{'role': 'user', 'content': prompt}],

        },

        timeout=240  # 2026-07-06: raised from 90s. This call does a full competitive
        # SWOT analysis (max_tokens=8000) over a substantial prompt (scraped website
        # content + Perplexity research) — genuinely a slow call, not a hung one.
        # 90s was already marginal; confirm the Gunicorn worker timeout (systemd
        # service config, not in this file) is also raised to match, or this will
        # still get killed upstream regardless of this value.

    )

    

    if response.status_code != 200:

        error_msg = response.json().get('error', {}).get('message', f'Anthropic API returned HTTP {response.status_code}')

        return {'error': error_msg}

    

    data = response.json()

    content = data['content'][0]['text']

    

    # Strip markdown code fences if present

    content = re.sub(r'^```(?:json)?\s*', '', content)

    content = re.sub(r'\s*```$', '', content)

    

    try:

        parsed = json.loads(content)

        return parsed

    except json.JSONDecodeError as e:

        return {'error': f'Failed to parse analysis JSON: {str(e)}'}





def research_competitor(db_conn, admin_cursor, name, url, progress=None):

    """Full research pipeline: Perplexity research + Claude analysis + store."""

    # Get API keys

    keys = get_api_keys(admin_cursor)

    

    if not keys.get('perplexity'):

        return {'error': 'Perplexity API key not configured. Go to Settings to add it.'}

    if not keys.get('anthropic'):

        return {'error': 'Anthropic API key not configured. Go to Settings to add it.'}

    

    # Step 1: Perplexity research

    if progress: progress('Researching the company across the web...')

    research_result = perplexity_research(keys['perplexity'], name, url)

    if 'error' in research_result:

        return research_result

    

    research_data = research_result['content']

    

    # Step 1b: Also fetch website content as supplementary data

    if progress: progress('Reading their website...')

    website_content = fetch_website_content(url)

    if website_content:

        research_data = f"## Direct Website Content\n{website_content}\n\n## Perplexity Research\n{research_data}"

    

    # Step 2: Claude analysis

    if progress: progress('Analyzing findings with Claude...')

    analysis = claude_analyze(keys['anthropic'], name, url, research_data)

    if 'error' in analysis:

        return analysis

    

    # Check for data mismatch

    if analysis.get('data_mismatch'):

        return {'error': f"Data mismatch detected: {analysis.get('mismatch_reason', 'Unknown reason')}"}

    

    # Step 3: Store in database

    if progress: progress('Saving results...')

    cursor = db_conn.cursor()

    

    def validate_threat_level(level):

        valid = ['critical', 'high', 'medium', 'low', 'watch']

        return level if level in valid else 'medium'

    

    data = {

        'company_name': analysis.get('company_name', name)[:255],

        'company_url': analysis.get('company_url', url)[:500],

        'tagline': analysis.get('tagline', '')[:500],

        'founded': analysis.get('founded', '')[:50],

        'headquarters': analysis.get('headquarters', '')[:255],

        'employee_count': analysis.get('employee_count', '')[:100],

        'funding_total': analysis.get('funding_total', '')[:100],

        'funding_stage': analysis.get('funding_stage', '')[:100],

        'pricing_summary': json.dumps(analysis.get('pricing_summary', {})),

        'platforms_supported': json.dumps(analysis.get('platforms_supported', [])),

        'key_features': json.dumps(analysis.get('key_features', [])),

        'swot_analysis': json.dumps(analysis.get('swot_analysis', {})),

        'competitive_score': int(analysis.get('competitive_score', 50)),

        'threat_level': validate_threat_level(analysis.get('threat_level', 'medium')),

        'strategy_recommendations': json.dumps(analysis.get('strategy_recommendations', [])),

        'feature_parity': json.dumps(analysis.get('feature_parity', {})),

        'raw_research': research_data,

        'raw_analysis': json.dumps(analysis),

        'last_scanned_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),

        'status': 'active',

    }

    

    columns = ', '.join(data.keys())

    placeholders = ', '.join(['%s'] * len(data))

    sql = f"INSERT INTO wp_citemetrix_competition ({columns}) VALUES ({placeholders})"

    

    cursor.execute(sql, list(data.values()))

    db_conn.commit()

    competitor_id = cursor.lastrowid

    

    return {'success': True, 'competitor_id': competitor_id, 'analysis': analysis}





def rescan_competitor(db_conn, admin_cursor, competitor_id, progress=None):

    """Rescan an existing competitor and detect changes."""

    cursor = db_conn.cursor()

    

    # Get existing competitor

    cursor.execute("SELECT * FROM wp_citemetrix_competition WHERE id = %s", (competitor_id,))

    old_data = cursor.fetchone()

    if not old_data:

        return {'error': 'Competitor not found'}

    

    # Get API keys

    keys = get_api_keys(admin_cursor)

    if not keys.get('perplexity') or not keys.get('anthropic'):

        return {'error': 'API keys not configured. Go to Settings to add them.'}

    

    name = old_data['company_name']

    url = old_data['company_url']

    

    # Run research pipeline

    if progress: progress('Researching the company across the web...')

    research_result = perplexity_research(keys['perplexity'], name, url)

    if 'error' in research_result:

        return research_result

    

    research_data = research_result['content']

    if progress: progress('Reading their website...')

    website_content = fetch_website_content(url)

    if website_content:

        research_data = f"## Direct Website Content\n{website_content}\n\n## Perplexity Research\n{research_data}"

    

    if progress: progress('Analyzing findings with Claude...')

    analysis = claude_analyze(keys['anthropic'], name, url, research_data)

    if 'error' in analysis:

        return analysis

    

    if analysis.get('data_mismatch'):

        return {'error': f"Data mismatch detected: {analysis.get('mismatch_reason', 'Unknown reason')}"}

    

    # Detect changes and store history

    changes = detect_changes(old_data, analysis)

    

    # Update competitor record

    def validate_threat_level(level):

        valid = ['critical', 'high', 'medium', 'low', 'watch']

        return level if level in valid else 'medium'

    

    update_data = {

        'tagline': analysis.get('tagline', '')[:500],

        'founded': analysis.get('founded', '')[:50],

        'headquarters': analysis.get('headquarters', '')[:255],

        'employee_count': analysis.get('employee_count', '')[:100],

        'funding_total': analysis.get('funding_total', '')[:100],

        'funding_stage': analysis.get('funding_stage', '')[:100],

        'pricing_summary': json.dumps(analysis.get('pricing_summary', {})),

        'platforms_supported': json.dumps(analysis.get('platforms_supported', [])),

        'key_features': json.dumps(analysis.get('key_features', [])),

        'swot_analysis': json.dumps(analysis.get('swot_analysis', {})),

        'competitive_score': int(analysis.get('competitive_score', 50)),

        'threat_level': validate_threat_level(analysis.get('threat_level', 'medium')),

        'strategy_recommendations': json.dumps(analysis.get('strategy_recommendations', [])),

        'feature_parity': json.dumps(analysis.get('feature_parity', {})),

        'raw_research': research_data,

        'raw_analysis': json.dumps(analysis),

        'last_scanned_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),

    }

    

    if progress: progress('Saving results and detecting changes...')

    set_clause = ', '.join([f"{k} = %s" for k in update_data.keys()])

    cursor.execute(f"UPDATE wp_citemetrix_competition SET {set_clause} WHERE id = %s", 

                   list(update_data.values()) + [competitor_id])

    

    # Store changes in history

    for change in changes:

        cursor.execute("""

            INSERT INTO wp_citemetrix_competition_history 

            (competitor_id, change_type, change_summary, previous_value, new_value, severity, detected_at)

            VALUES (%s, %s, %s, %s, %s, %s, NOW())

        """, (competitor_id, change['type'], change['summary'], 

              change.get('previous'), change.get('new'), change.get('severity', 'info')))

    

    db_conn.commit()

    

    return {'success': True, 'changes': changes, 'analysis': analysis}





def detect_changes(old_data, new_analysis):

    """Compare old and new data to detect significant changes."""

    changes = []

    

    # Parse old JSON fields

    def parse_json(val):

        if isinstance(val, (dict, list)):

            return val

        if val:

            try:

                return json.loads(val)

            except:

                pass

        return None

    

    # Check threat level change

    old_threat = old_data.get('threat_level', 'medium')

    new_threat = new_analysis.get('threat_level', 'medium')

    if old_threat != new_threat:

        threat_order = {'watch': 0, 'low': 1, 'medium': 2, 'high': 3, 'critical': 4}

        severity = 'high' if threat_order.get(new_threat, 2) > threat_order.get(old_threat, 2) else 'info'

        changes.append({

            'type': 'threat_level_change',

            'summary': f'Threat level changed from {old_threat} to {new_threat}',

            'previous': old_threat,

            'new': new_threat,

            'severity': severity

        })

    

    # Check competitive score change

    old_score = old_data.get('competitive_score', 50)

    new_score = new_analysis.get('competitive_score', 50)

    if abs(old_score - new_score) >= 10:

        severity = 'high' if new_score > old_score else 'medium'

        changes.append({

            'type': 'score_change',

            'summary': f'Competitive score changed from {old_score} to {new_score}',

            'previous': str(old_score),

            'new': str(new_score),

            'severity': severity

        })

    

    # Check funding change

    old_funding = old_data.get('funding_total', '')

    new_funding = new_analysis.get('funding_total', '')

    if old_funding != new_funding and new_funding:

        changes.append({

            'type': 'funding_change',

            'summary': f'Funding updated: {new_funding}',

            'previous': old_funding,

            'new': new_funding,

            'severity': 'high'

        })

    

    # Check for new features they have that we don't

    old_parity = parse_json(old_data.get('feature_parity')) or {}

    new_parity = new_analysis.get('feature_parity', {})

    

    old_gaps = set(f.get('feature', '') if isinstance(f, dict) else str(f) 

                   for f in old_parity.get('they_have_we_dont', []))

    new_gaps = set(f.get('feature', '') if isinstance(f, dict) else str(f) 

                   for f in new_parity.get('they_have_we_dont', []))

    

    new_features = new_gaps - old_gaps

    if new_features:

        changes.append({

            'type': 'new_feature_gap',

            'summary': f'New features they have that we don\'t: {", ".join(list(new_features)[:3])}',

            'previous': None,

            'new': ', '.join(new_features),

            'severity': 'medium'

        })

    

    return changes

