"""
CiteMetrix Canonical Features Inventory — Python module form.

This is the single source of truth for what CiteMetrix can do today,
formatted for use as Claude system context in competitive analysis.

When CiteMetrix capabilities change, update CITEMETRIX_CONTEXT below.
This module is imported by competition.py via get_citemetrix_context().

Source document: citemetrix-features-inventory-v1.md (2026-05-01)
Last refreshed: 2026-05-22 (plugin 4.8.3) — corrected fundraise, platform
count (9, removed Meta AI), team roles; added post-4.0 features
(Activity Tracking, ROI Analysis, Citation Insight). KEEP THIS UPDATED
ON EACH RELEASE — add "refresh citemetrix_inventory.py" to the release checklist.
"""

# Single source of truth — keep in sync with the live product on each release.
CITEMETRIX_CONTEXT = """CiteMetrix is an AI visibility analytics platform (SaaS). Compare the competitor ONLY against the features listed below. If the competitor has a feature that matches any item below, it belongs in 'both_have', NOT 'they_have_we_dont'.

CORE PLATFORM:

- Monitors brand citations across 9 AI platforms out of the box on every plan: ChatGPT (OpenAI), Perplexity, Claude (Anthropic), Google Gemini, Grok (xAI), Microsoft Copilot, DeepSeek, Mistral AI, Google AI Overviews (via DataForSEO)
- Platform Registry architecture: unlimited custom AI platforms can be added via configuration — any API-accessible model
- No per-platform tiering — all 9 native platforms available on Starter through Enterprise tiers
- API-based scanning (not scraping) — every query is submitted via authenticated API call, reproducible and ToS-compliant
- BYOK (Bring Your Own Key) model: users supply their own API keys, giving 85-90% gross margins
- Intent-stratified query model with five tiers: Direct Brand, Branded Category, Unbranded Category, Problem/Solution, and Fanout queries (sub-questions AI decomposes complex queries into — unique to CiteMetrix)

MODELSCORE COMPOSITE METRIC:

- ModelScore (trademarked) is a 0-100 composite AI visibility metric
- Four weighted components: Citation Score (35%, from CiteMetrix scan data), Brand Demand (25%, from Google Search Console OAuth), Authority Transfer (20%, from GA4 OAuth — Adobe Analytics available via ESC sister-platform for AEM clients), Technical Readiness (20%, from CiteMetrix crawler + PageSpeed API)
- No other AI visibility platform publishes a composite score — Profound, Peec AI, Otterly, Brandlight all report citation frequency by platform only

BRAND FACTS AND ACCURACY DETECTION:

- Structured Brand Facts library: manually loaded, JSON-imported, or AI-suggested from public brand content
- Facts categorized, severity-flagged (Critical, Moderate, Minor), and versioned with audit trail
- Semantic comparison engine using Claude — catches paraphrased inaccuracies, implied falsehoods, misleading omissions, not just verbatim mismatches
- Issue types tracked: Critical Omission, Incorrect Fact, Misleading Context, Fabricated Claim, Outdated Information
- Hallucination Watch (closed-loop verification): open accuracy issues are automatically re-checked daily, auto-resolved and timestamped when AI corrects itself
- For regulated industries (pharmaceutical, healthcare, financial, legal) this is the compliance layer the market lacks

MONITORING AND ANALYSIS:

- Citation Tracker: tracks when and where AI platforms mention your brand, per platform, per query
- Branded vs unbranded query split tracked separately — branded citation rate vs unbranded citation rate per platform and in aggregate
- Sentiment Analysis: positive/neutral/negative sentiment + framing (recommended, mentioned, named as comparison) tracked over time
- Share of Voice (SOV): your cited responses as a percentage of all cited responses (yours + competitors'), per platform per query tier
- Citation Insight panel: per-query coordinated view — platform coverage heatmap (sentiment-encoded), citation position trend, and an actions/event timeline overlay
- Competitor Tracker: head-to-head citation rate, SOV, and sentiment per competitor per platform
- AI Crawl Intelligence: server log analysis identifying AI bot visits (GPTBot, ClaudeBot, PerplexityBot, Google-Extended, etc.)
- HCP + DTC audience split: separate query sets and tracking for healthcare professional vs direct-to-consumer audiences
- Monitored Prompts / Custom Prompt Builder: users create and track custom queries
- Timeline Annotations: mark events on analytics timeline (algorithm changes, product launches, content publishes)

ACTION-OUTCOME TRACKING (Activity Tracking — measures intervention efficacy; no competitor does this):

- Closes the observe → act → measure loop: every actionable observation (citation gap, brand fact, source page, content refresh, AI crawler issue) can be tracked when a team member acts on it
- Each tracked action gets an automatic outcome computer that measures the relevant metric (citation rate, hallucination count, crawler score) in a before/after window from subsequent scans, attributing the change to the action
- Send-to-teammate assignment with email notification; Recent Actions timeline with filters, reassign, mark-complete
- This is measurement of intervention efficacy — not project management — squarely in the AI-visibility lane, and unique in the market

ROI ANALYSIS:

- Translates AI search visibility into business value for CMO/CEO/board defense
- Cohort Movement view: four outcome cohorts (Brand Visibility, Citation Quality, Share of Voice, Referral Impact) comparing the current period to the prior equal-length period
- Cost-input ROI calculator with industry benchmarks (agency/tools/staff cost vs visibility gains)

REMEDIATION TOOLS (10 built-in fix-it tools — competitors typically only monitor; we monitor AND fix):

- llms.txt Generator: builds and maintains llms.txt file, the emerging standard for giving AI structured brand information
- Schema Generator: generates JSON-LD structured data for pages based on actual content type
- Robots Analyzer: verifies robots.txt and meta directives aren't blocking AI crawlers
- FAQ Generator: generates AI-optimized FAQ content targeting unbranded queries with 0% citation rate
- AI Content Audit: bulk evaluation of existing content against AI citation readiness criteria
- Schema Advisor: recommends structured data schema based on actual page content
- Content Optimizer: analyzes existing pages against the queries they're failing to be cited on, recommends specific changes
- EEAT Audit: evaluates content against Google's Experience, Expertise, Authoritativeness, Trustworthiness framework
- Content Refresh Advisor: identifies which existing pages need updates based on age, performance decay, and competitor activity
- Content Engine: AI-powered content generation informed by your citation data — identifies content opportunities from citation gaps, competitor wins, declining queries, and sentiment issues, then generates publication-ready content (full article, brief, SEO metadata, schema markup, image script). Brand context injection automatically incorporates verified Brand Facts, recent AI platform responses, and competitor data. Includes direct WordPress publishing via REST API and Application Passwords (configured per-domain in Domain Settings) — content publishes as draft posts with Yoast SEO title and meta description populated. Multi-format export also supports Markdown, HTML, and clipboard. Provides a full diagnose-to-publish workflow including content brief generation as one section of its output.
- Closed-loop workflow: identify gap → generate fix → publish to CMS → Hallucination Watch confirms AI updated its response. Only end-to-end workflow in the AI visibility market.

AI COACHING SYSTEM (three coaching touchpoints, zero configuration):

- First-scan debrief email: personalized email within 60 seconds of first scan with exact ModelScore, platform gaps, and one specific action — uses real data, not generic tips
- In-app coaching card: surfaces at top of dashboard after first scan with three AI-generated, data-specific next steps naming the exact CiteMetrix tool to use
- Weekly AI consultant briefing: Claude-written paragraph in every weekly email analyzing user's specific data — what moved, why, and the single most important action for the coming week

MOBILE-NATIVE STACK (shipped April 28, 2026 — the first AI visibility platform with voice input and real-time push on iOS at any tier):

- Progressive Web App at app.citemetrix.com — installable from any modern mobile browser to device home screen, no App Store, no Google Play
- Same authentication and data layer as web dashboard, seamless desktop-to-mobile movement
- Service Worker caches static assets and last-known data for offline-tolerant operation
- Native iOS standalone mode with status bar styling, safe-area handling, no browser chrome
- Real-time push notifications via VAPID (the protocol Apple mandated for iOS 17.4+ PWA push)
- Push events on: scan_complete, scan_failed, score_drop (10% relative or 3-point floor), score_rise, accuracy_issue_detected, competition_alert_created, key_health (API key issues)
- Severity-aware policy: critical and warn severities push immediately; info and success are feed-only
- CiteMetrix Analyst (trademarked) on mobile: per-alert Instant Brief auto-generates on first tap, threaded conversations persist across sessions and devices
- Voice input on the Analyst: Web Speech API integration, live interim transcripts, augments typed text — never auto-submits
- JWT-HS256 session tokens with sliding refresh

CITEMETRIX ANALYST (web + mobile):

- Persistent, interactive AI analysis layer embedded across all dashboards
- Each dashboard has a dedicated AI agent that reads current data, generates an opening Instant Brief automatically, maintains threaded conversation across sessions
- Conversations scoped to domain with full user attribution, stored indefinitely, exportable as PDF or JSON for compliance and audit
- Powered by Claude on customer's BYOK Anthropic API key — no platform usage limits, no per-query upcharge

REPORTING:

- 5 report types: AI Visibility Report, Monthly Progress Report, Competitive Intelligence Report, Content Opportunity Report, AI Visibility Audit Report (6-dimension scored baseline diagnostic)
- White-label PDF reports on Agency tier and above
- Generate Presentation: domain data converted to branded PowerPoint automatically — real numbers, CiteMetrix styling, zero manual work. No competitor offers this.
- Weekly email digests with key metric changes
- Real-time alert system for significant changes (push to mobile + web feed)
- CSV/data export capabilities

INTEGRATIONS:

- Google Search Console: native OAuth, feeds Brand Demand component of ModelScore
- Google Analytics 4: native OAuth, feeds Authority Transfer component of ModelScore
- Adobe Analytics: available via Expert SEO Consulting (ESC) sister-platform infrastructure for clients on Adobe AEM (white-glove setup; native OAuth on Q3 2026 roadmap)
- DataForSEO: powers Google AI Overviews tracking and SERP analysis

SECURITY AND ENTERPRISE:

- SOC 2 documentation complete; formal audit planned post-funding
- Team management with role-based access: an account-permission role enum (admin, sales, marketing, support, operations) layered with a view-persona axis (Generalist, Marketing, Analyst, Engineer) that controls which tools each team member sees; Account Owner override controls at account-default and per-user level
- Per-domain access controls allowing agency teams to scope team members to specific client domains
- Two-factor authentication (TOTP + backup codes) on all accounts
- AES-256-CBC encryption on all stored API keys
- Comprehensive audit logging of all user actions and data access events; admin audit portal with paginated, expandable change history
- Owner transfer capability
- BYOK architecture: client data does not flow through CiteMetrix infrastructure beyond the AI platform itself — critical for enterprise data residency

PLANS AND PRICING:

- Starter: $79/mo — 1 domain, 1 user seat, all 9 AI platforms, BYOK unlimited
- Professional: $199/mo — 5 domains, 3 user seats, native GSC + GA4 OAuth, CiteMetrix Analyst on demand, per-domain access controls
- Agency: $499/mo — 15 domains, 10 user seats, white-label PDF + presentations, multi-brand management
- Enterprise: Custom — unlimited domains, SSO, SLA, dedicated onboarding, Adobe Analytics integration available
- All plans include all 9 AI platforms, BYOK, full mobile + voice + push

BUSINESS:

- Beta-to-paid transition complete May 1, 2026 — paying customers active; beta program ran 39 participants across 48 domains generating 100K+ citations
- Production plugin on a rapid release cadence (4.x series; multiple releases per month)
- Mobile-native stack shipped April 28, 2026
- $0 external funding to date (bootstrapped)
- Raising $1.75M at $5M pre-money valuation to fund three key hires (technical lead, sales, customer success/onboarding)
- Founder: Eric Richmond, 30+ years in digital marketing, based in Norwalk, CT
- Sister platform: Expert SEO Consulting (47-tool SEO platform with active paying subscribers)"""


def get_citemetrix_context():
    """Return CiteMetrix platform description for competitive analysis prompts.

    This is the canonical context used by the SWOT analyzer in competition.py.
    Source of truth: citemetrix-features-inventory-v1.md
    """
    return CITEMETRIX_CONTEXT
