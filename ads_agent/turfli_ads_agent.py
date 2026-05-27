"""
Turfli Ads Agent
================
A Claude-powered agent that analyzes and optimizes Google Ads and Meta Ads
campaigns for Turfli (turfli.com). Run weekly or on-demand.

Setup:
  pip install -r requirements.txt
  Copy config.example.yml → config.yml and fill in credentials.
  python turfli_ads_agent.py [--platform google|meta|both] [--action analyze|optimize|report]
"""

import os
import sys
import json
import yaml
import argparse
import datetime
from typing import Any

import anthropic

# ── Optional API clients ─────────────────────────────────────────────────────
# Install google-ads and facebook-business only if using those platforms.
try:
    from google.ads.googleads.client import GoogleAdsClient
    from google.ads.googleads.errors import GoogleAdsException
    GOOGLE_ADS_AVAILABLE = True
except ImportError:
    GOOGLE_ADS_AVAILABLE = False

try:
    from facebook_business.api import FacebookAdsApi
    from facebook_business.adobjects.adaccount import AdAccount
    from facebook_business.adobjects.campaign import Campaign
    from facebook_business.adobjects.adset import AdSet
    from facebook_business.adobjects.ad import Ad
    META_ADS_AVAILABLE = True
except ImportError:
    META_ADS_AVAILABLE = False


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path: str = "config.yml") -> dict:
    config_path = os.path.join(os.path.dirname(__file__), path)
    if not os.path.exists(config_path):
        print(f"[ERROR] config.yml not found at {config_path}. Copy config.example.yml → config.yml.")
        sys.exit(1)
    with open(config_path) as f:
        return yaml.safe_load(f)


# ── Google Ads tools ──────────────────────────────────────────────────────────

def google_get_campaign_performance(client: Any, customer_id: str, days: int = 30) -> dict:
    """Fetch campaign-level performance for the last N days."""
    ga_service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT
            campaign.id,
            campaign.name,
            campaign.status,
            campaign.bidding_strategy_type,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.ctr,
            metrics.average_cpc
        FROM campaign
        WHERE segments.date DURING LAST_{days}_DAYS
        ORDER BY metrics.cost_micros DESC
    """
    results = []
    try:
        response = ga_service.search(customer_id=customer_id, query=query)
        for row in response:
            c = row.campaign
            m = row.metrics
            results.append({
                "id": str(c.id),
                "name": c.name,
                "status": c.status.name,
                "bidding_strategy": c.bidding_strategy_type.name,
                "impressions": m.impressions,
                "clicks": m.clicks,
                "cost_usd": round(m.cost_micros / 1_000_000, 2),
                "conversions": round(m.conversions, 1),
                "conversion_value": round(m.conversions_value, 2),
                "ctr_pct": round(m.ctr * 100, 2),
                "avg_cpc_usd": round(m.average_cpc / 1_000_000, 2),
            })
    except GoogleAdsException as ex:
        return {"error": str(ex)}
    return {"campaigns": results, "days": days}


def google_get_keyword_performance(client: Any, customer_id: str, days: int = 30) -> dict:
    """Fetch top 50 keywords by spend."""
    ga_service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT
            ad_group_criterion.keyword.text,
            ad_group_criterion.keyword.match_type,
            campaign.name,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.ctr,
            metrics.average_cpc,
            metrics.search_impression_share
        FROM keyword_view
        WHERE segments.date DURING LAST_{days}_DAYS
          AND ad_group_criterion.status != 'REMOVED'
        ORDER BY metrics.cost_micros DESC
        LIMIT 50
    """
    results = []
    try:
        response = ga_service.search(customer_id=customer_id, query=query)
        for row in response:
            k = row.ad_group_criterion.keyword
            m = row.metrics
            results.append({
                "keyword": k.text,
                "match_type": k.match_type.name,
                "campaign": row.campaign.name,
                "impressions": m.impressions,
                "clicks": m.clicks,
                "cost_usd": round(m.cost_micros / 1_000_000, 2),
                "conversions": round(m.conversions, 1),
                "ctr_pct": round(m.ctr * 100, 2),
                "avg_cpc_usd": round(m.average_cpc / 1_000_000, 2),
                "impression_share_pct": round((m.search_impression_share or 0) * 100, 1),
            })
    except GoogleAdsException as ex:
        return {"error": str(ex)}
    return {"keywords": results}


def google_update_campaign_budget(client: Any, customer_id: str, campaign_id: str, new_budget_usd: float) -> dict:
    """Update a campaign's daily budget."""
    campaign_budget_service = client.get_service("CampaignBudgetService")
    campaign_service = client.get_service("CampaignService")
    try:
        # Get budget resource name for this campaign
        query = f"""
            SELECT campaign.campaign_budget
            FROM campaign
            WHERE campaign.id = {campaign_id}
        """
        ga_service = client.get_service("GoogleAdsService")
        response = ga_service.search(customer_id=customer_id, query=query)
        rows = list(response)
        if not rows:
            return {"error": f"Campaign {campaign_id} not found"}
        budget_resource = rows[0].campaign.campaign_budget

        # Update it
        from google.protobuf import field_mask_pb2
        budget_operation = client.get_type("CampaignBudgetOperation")
        budget = budget_operation.update
        budget.resource_name = budget_resource
        budget.amount_micros = int(new_budget_usd * 1_000_000)
        budget_operation.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["amount_micros"]))
        campaign_budget_service.mutate_campaign_budgets(
            customer_id=customer_id,
            operations=[budget_operation]
        )
        return {"success": True, "campaign_id": campaign_id, "new_budget_usd": new_budget_usd}
    except Exception as ex:
        return {"error": str(ex)}


def google_pause_campaign(client: Any, customer_id: str, campaign_id: str) -> dict:
    """Pause a campaign."""
    campaign_service = client.get_service("CampaignService")
    try:
        campaign_operation = client.get_type("CampaignOperation")
        campaign = campaign_operation.update
        campaign.resource_name = campaign_service.campaign_path(customer_id, campaign_id)
        campaign.status = client.enums.CampaignStatusEnum.PAUSED
        from google.protobuf import field_mask_pb2
        campaign_operation.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))
        campaign_service.mutate_campaigns(customer_id=customer_id, operations=[campaign_operation])
        return {"success": True, "campaign_id": campaign_id, "new_status": "PAUSED"}
    except Exception as ex:
        return {"error": str(ex)}


# ── Meta Ads tools ────────────────────────────────────────────────────────────

def meta_get_campaign_performance(account: Any, days: int = 30) -> dict:
    """Fetch Meta campaign performance."""
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    until = datetime.date.today().isoformat()
    try:
        campaigns = account.get_campaigns(fields=[
            Campaign.Field.id,
            Campaign.Field.name,
            Campaign.Field.status,
            Campaign.Field.objective,
        ])
        results = []
        for camp in campaigns:
            insights = camp.get_insights(params={
                "time_range": {"since": since, "until": until},
                "fields": "impressions,clicks,spend,actions,ctr,cpc",
            })
            if insights:
                ins = insights[0]
                leads = sum(
                    int(a["value"]) for a in (ins.get("actions") or [])
                    if a["action_type"] in ("lead", "offsite_conversion.fb_pixel_lead")
                )
                results.append({
                    "id": camp["id"],
                    "name": camp["name"],
                    "status": camp["status"],
                    "objective": camp.get("objective", ""),
                    "impressions": int(ins.get("impressions", 0)),
                    "clicks": int(ins.get("clicks", 0)),
                    "spend_usd": round(float(ins.get("spend", 0)), 2),
                    "leads": leads,
                    "ctr_pct": round(float(ins.get("ctr", 0)) * 100, 2),
                    "cpc_usd": round(float(ins.get("cpc", 0)), 2),
                })
        return {"campaigns": results, "days": days}
    except Exception as ex:
        return {"error": str(ex)}


def meta_get_ad_creative_performance(account: Any, days: int = 30) -> dict:
    """Fetch ad-level performance to find top and bottom creatives."""
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    until = datetime.date.today().isoformat()
    try:
        ads = account.get_ads(fields=[Ad.Field.id, Ad.Field.name, Ad.Field.status])
        results = []
        for ad in ads[:50]:  # cap at 50
            insights = ad.get_insights(params={
                "time_range": {"since": since, "until": until},
                "fields": "impressions,clicks,spend,actions,ctr",
            })
            if insights:
                ins = insights[0]
                leads = sum(
                    int(a["value"]) for a in (ins.get("actions") or [])
                    if a["action_type"] in ("lead", "offsite_conversion.fb_pixel_lead")
                )
                results.append({
                    "id": ad["id"],
                    "name": ad["name"],
                    "status": ad["status"],
                    "impressions": int(ins.get("impressions", 0)),
                    "clicks": int(ins.get("clicks", 0)),
                    "spend_usd": round(float(ins.get("spend", 0)), 2),
                    "leads": leads,
                    "ctr_pct": round(float(ins.get("ctr", 0)) * 100, 2),
                    "cost_per_lead": round(float(ins.get("spend", 0)) / max(leads, 1), 2),
                })
        results.sort(key=lambda x: x["cost_per_lead"])
        return {"ads": results}
    except Exception as ex:
        return {"error": str(ex)}


def meta_update_campaign_budget(account: Any, campaign_id: str, daily_budget_usd: float) -> dict:
    """Update a Meta campaign daily budget."""
    try:
        campaign = Campaign(campaign_id)
        campaign.api_update(fields=[], params={"daily_budget": int(daily_budget_usd * 100)})
        return {"success": True, "campaign_id": campaign_id, "daily_budget_usd": daily_budget_usd}
    except Exception as ex:
        return {"error": str(ex)}


def meta_pause_campaign(account: Any, campaign_id: str) -> dict:
    """Pause a Meta campaign."""
    try:
        campaign = Campaign(campaign_id)
        campaign.api_update(fields=[], params={"status": Campaign.Status.paused})
        return {"success": True, "campaign_id": campaign_id, "new_status": "PAUSED"}
    except Exception as ex:
        return {"error": str(ex)}


# ── Mock data for dry-run / testing ──────────────────────────────────────────

MOCK_GOOGLE_DATA = {
    "campaigns": [
        {"id": "1001", "name": "Scottsdale Artificial Turf | Search", "status": "ENABLED",
         "bidding_strategy": "TARGET_CPA", "impressions": 12400, "clicks": 487,
         "cost_usd": 1842.10, "conversions": 18.0, "conversion_value": 0, "ctr_pct": 3.93, "avg_cpc_usd": 3.78},
        {"id": "1002", "name": "Backyard Remodel Scottsdale | Search", "status": "ENABLED",
         "bidding_strategy": "MAXIMIZE_CONVERSIONS", "impressions": 8700, "clicks": 210,
         "cost_usd": 1105.40, "conversions": 6.0, "conversion_value": 0, "ctr_pct": 2.41, "avg_cpc_usd": 5.26},
        {"id": "1003", "name": "Putting Green | Search", "status": "ENABLED",
         "bidding_strategy": "MAXIMIZE_CONVERSIONS", "impressions": 3200, "clicks": 98,
         "cost_usd": 412.80, "conversions": 8.0, "conversion_value": 0, "ctr_pct": 3.06, "avg_cpc_usd": 4.21},
        {"id": "1004", "name": "Paver Patio Phoenix | Search", "status": "ENABLED",
         "bidding_strategy": "TARGET_CPA", "impressions": 1100, "clicks": 22,
         "cost_usd": 187.20, "conversions": 0.5, "conversion_value": 0, "ctr_pct": 2.00, "avg_cpc_usd": 8.51},
    ],
    "days": 30,
}

MOCK_GOOGLE_KEYWORDS = {
    "keywords": [
        {"keyword": "artificial turf scottsdale", "match_type": "EXACT", "campaign": "Scottsdale Artificial Turf | Search",
         "impressions": 2800, "clicks": 142, "cost_usd": 522.50, "conversions": 6.0, "ctr_pct": 5.07, "avg_cpc_usd": 3.68, "impression_share_pct": 62.0},
        {"keyword": "backyard turf installation", "match_type": "PHRASE", "campaign": "Scottsdale Artificial Turf | Search",
         "impressions": 1900, "clicks": 88, "cost_usd": 330.40, "conversions": 4.0, "ctr_pct": 4.63, "avg_cpc_usd": 3.75, "impression_share_pct": 44.0},
        {"keyword": "backyard remodel scottsdale", "match_type": "EXACT", "campaign": "Backyard Remodel Scottsdale | Search",
         "impressions": 1400, "clicks": 62, "cost_usd": 390.20, "conversions": 3.0, "ctr_pct": 4.43, "avg_cpc_usd": 6.29, "impression_share_pct": 38.0},
        {"keyword": "outdoor kitchen scottsdale", "match_type": "BROAD", "campaign": "Backyard Remodel Scottsdale | Search",
         "impressions": 980, "clicks": 14, "cost_usd": 118.30, "conversions": 0.0, "ctr_pct": 1.43, "avg_cpc_usd": 8.45, "impression_share_pct": 21.0},
        {"keyword": "putting green backyard", "match_type": "PHRASE", "campaign": "Putting Green | Search",
         "impressions": 2100, "clicks": 71, "cost_usd": 298.60, "conversions": 6.0, "ctr_pct": 3.38, "avg_cpc_usd": 4.21, "impression_share_pct": 55.0},
    ]
}

MOCK_META_DATA = {
    "campaigns": [
        {"id": "201", "name": "Retargeting — Website Visitors 30d", "status": "ACTIVE",
         "objective": "LEAD_GENERATION", "impressions": 28000, "clicks": 420, "spend_usd": 640.00, "leads": 14, "ctr_pct": 1.50, "cpc_usd": 1.52},
        {"id": "202", "name": "Prospecting — Scottsdale Homeowners $500k+", "status": "ACTIVE",
         "objective": "LEAD_GENERATION", "impressions": 85000, "clicks": 680, "spend_usd": 1220.00, "leads": 9, "ctr_pct": 0.80, "cpc_usd": 1.79},
        {"id": "203", "name": "Video — Before/After Backyard Remodel", "status": "ACTIVE",
         "objective": "VIDEO_VIEWS", "impressions": 42000, "clicks": 310, "spend_usd": 480.00, "leads": 3, "ctr_pct": 0.74, "cpc_usd": 1.55},
    ],
    "days": 30,
}


# ── Claude agent ──────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "get_google_campaign_performance",
        "description": "Get Google Ads campaign performance data for the last N days.",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "default": 30}},
            "required": [],
        },
    },
    {
        "name": "get_google_keyword_performance",
        "description": "Get Google Ads keyword-level performance data.",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "default": 30}},
            "required": [],
        },
    },
    {
        "name": "update_google_campaign_budget",
        "description": "Update the daily budget for a Google Ads campaign.",
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign_id": {"type": "string"},
                "new_budget_usd": {"type": "number"},
                "reason": {"type": "string"},
            },
            "required": ["campaign_id", "new_budget_usd", "reason"],
        },
    },
    {
        "name": "pause_google_campaign",
        "description": "Pause a Google Ads campaign.",
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["campaign_id", "reason"],
        },
    },
    {
        "name": "get_meta_campaign_performance",
        "description": "Get Meta (Facebook/Instagram) campaign performance data.",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "default": 30}},
            "required": [],
        },
    },
    {
        "name": "get_meta_ad_performance",
        "description": "Get Meta individual ad creative performance to find top/bottom performers.",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "default": 30}},
            "required": [],
        },
    },
    {
        "name": "update_meta_campaign_budget",
        "description": "Update the daily budget for a Meta campaign.",
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign_id": {"type": "string"},
                "daily_budget_usd": {"type": "number"},
                "reason": {"type": "string"},
            },
            "required": ["campaign_id", "daily_budget_usd", "reason"],
        },
    },
    {
        "name": "pause_meta_campaign",
        "description": "Pause a Meta campaign.",
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["campaign_id", "reason"],
        },
    },
    {
        "name": "generate_ad_copy",
        "description": "Generate new ad copy variants for Google or Meta ads targeting Turfli services.",
        "input_schema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ["google", "meta"]},
                "service": {"type": "string", "description": "e.g. artificial turf, backyard remodel, putting green"},
                "location": {"type": "string", "description": "e.g. Scottsdale, Paradise Valley"},
                "ad_type": {"type": "string", "description": "e.g. responsive search ad, Facebook lead ad, Instagram story"},
                "num_variants": {"type": "integer", "default": 3},
            },
            "required": ["platform", "service"],
        },
    },
    {
        "name": "generate_weekly_report",
        "description": "Generate a formatted weekly performance report for the Turfli owner.",
        "input_schema": {
            "type": "object",
            "properties": {
                "include_recommendations": {"type": "boolean", "default": True},
            },
            "required": [],
        },
    },
]


def execute_tool(name: str, inputs: dict, config: dict, google_client=None, meta_account=None, dry_run: bool = True) -> str:
    """Execute a tool call. In dry-run mode, returns mock data."""

    if name == "get_google_campaign_performance":
        if dry_run or not google_client:
            return json.dumps(MOCK_GOOGLE_DATA)
        return json.dumps(google_get_campaign_performance(google_client, config["google"]["customer_id"], inputs.get("days", 30)))

    elif name == "get_google_keyword_performance":
        if dry_run or not google_client:
            return json.dumps(MOCK_GOOGLE_KEYWORDS)
        return json.dumps(google_get_keyword_performance(google_client, config["google"]["customer_id"], inputs.get("days", 30)))

    elif name == "update_google_campaign_budget":
        if dry_run:
            return json.dumps({"dry_run": True, "would_update": inputs})
        return json.dumps(google_update_campaign_budget(google_client, config["google"]["customer_id"], inputs["campaign_id"], inputs["new_budget_usd"]))

    elif name == "pause_google_campaign":
        if dry_run:
            return json.dumps({"dry_run": True, "would_pause": inputs["campaign_id"], "reason": inputs["reason"]})
        return json.dumps(google_pause_campaign(google_client, config["google"]["customer_id"], inputs["campaign_id"]))

    elif name == "get_meta_campaign_performance":
        if dry_run or not meta_account:
            return json.dumps(MOCK_META_DATA)
        return json.dumps(meta_get_campaign_performance(meta_account, inputs.get("days", 30)))

    elif name == "get_meta_ad_performance":
        if dry_run or not meta_account:
            return json.dumps({"ads": [
                {"id": "301", "name": "Before/After Turf — Scottsdale", "status": "ACTIVE",
                 "impressions": 18000, "clicks": 290, "spend_usd": 310.00, "leads": 7, "ctr_pct": 1.61, "cost_per_lead": 44.29},
                {"id": "302", "name": "Putting Green Promo — PV Homeowners", "status": "ACTIVE",
                 "impressions": 12000, "clicks": 130, "spend_usd": 170.00, "leads": 2, "ctr_pct": 1.08, "cost_per_lead": 85.00},
            ]})
        return json.dumps(meta_get_ad_creative_performance(meta_account, inputs.get("days", 30)))

    elif name == "update_meta_campaign_budget":
        if dry_run:
            return json.dumps({"dry_run": True, "would_update": inputs})
        return json.dumps(meta_update_campaign_budget(meta_account, inputs["campaign_id"], inputs["daily_budget_usd"]))

    elif name == "pause_meta_campaign":
        if dry_run:
            return json.dumps({"dry_run": True, "would_pause": inputs["campaign_id"], "reason": inputs["reason"]})
        return json.dumps(meta_pause_campaign(meta_account, inputs["campaign_id"]))

    elif name == "generate_ad_copy":
        # This is handled directly by Claude — return a signal to generate inline
        return json.dumps({"generate": True, "params": inputs})

    elif name == "generate_weekly_report":
        return json.dumps({"generate_report": True})

    return json.dumps({"error": f"Unknown tool: {name}"})


SYSTEM_PROMPT = """You are the dedicated Google Ads and Meta Ads optimization agent for Turfli (turfli.com).

Turfli is a luxury outdoor living design-build contractor based in Scottsdale, Arizona. Services include:
- Complete backyard remodels ($40k–$120k+)
- Landscape design + 3D renderings
- Artificial turf installation (premium, Smartscape certified)
- Putting greens
- Paver patios (travertine, porcelain)
- Outdoor kitchens and BBQ islands
- Pergolas and shade structures
- Pet turf
- Landscape lighting

Target customers: Scottsdale, Paradise Valley, and Phoenix metro homeowners in luxury neighborhoods (Silverleaf, DC Ranch, Gainey Ranch, McCormick Ranch, Troon North, Arcadia, Biltmore).

Your goals:
1. ANALYZE campaign performance across Google Ads and Meta Ads
2. IDENTIFY underperforming campaigns (high CPA, low conversion rate, low impression share)
3. RECOMMEND and EXECUTE optimizations: budget shifts, pauses, bid adjustments
4. GENERATE high-quality ad copy that matches Turfli's premium brand voice
5. PRODUCE clear weekly reports for the Turfli owner

Brand voice for ad copy:
- Premium and design-forward, NOT generic turf installer language
- Specific to Scottsdale and luxury neighborhoods
- Emphasizes design process, 3D renderings, 15-year warranty, ROC licensed, BBB A+
- CTAs: "Free Design Consultation", "See Your Backyard Before We Build It", "Request a Quote"

Key benchmarks for Turfli:
- Target CPA (cost per lead): $80–$150 for turf/patio, $120–$200 for full remodels
- Good CTR for Google Search: 4%+ is strong, below 2% needs attention
- Meta lead ad cost per lead: under $80 is good
- Always prioritize backyard remodel and landscape design campaigns (highest revenue potential)

When dry_run is True, all budget changes and pauses will be simulated — describe what you would do and why.
Always explain your reasoning before executing any action."""


def run_agent(config: dict, platform: str = "both", action: str = "analyze", dry_run: bool = True):
    """Main agent loop."""
    client_anthropic = anthropic.Anthropic(api_key=config.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY"))

    # Initialize platform clients
    google_client = None
    meta_account = None

    if not dry_run:
        if platform in ("google", "both") and GOOGLE_ADS_AVAILABLE:
            google_client = GoogleAdsClient.load_from_dict(config.get("google", {}))
        if platform in ("meta", "both") and META_ADS_AVAILABLE:
            FacebookAdsApi.init(access_token=config["meta"]["access_token"])
            meta_account = AdAccount(f"act_{config['meta']['account_id']}")

    # Build initial user message based on action
    today = datetime.date.today().strftime("%B %d, %Y")
    if action == "analyze":
        user_msg = f"Today is {today}. Analyze the last 30 days of performance for {'Google Ads' if platform == 'google' else 'Meta Ads' if platform == 'meta' else 'both Google Ads and Meta Ads'}. Pull all available data and give me a complete performance breakdown with your analysis."
    elif action == "optimize":
        user_msg = f"Today is {today}. Pull all campaign performance data for {'Google Ads and Meta Ads' if platform == 'both' else platform + ' Ads'}, then execute any optimizations you recommend. {'This is a dry run — simulate all changes and explain what you would do.' if dry_run else 'Apply the changes.'}"
    elif action == "report":
        user_msg = f"Today is {today}. Pull all campaign data and generate a complete weekly performance report for the Turfli owner. Include: overall spend and leads, top performers, underperformers, key insights, and specific action items for next week."
    elif action == "copy":
        user_msg = f"Generate 3 new Google Search ad variants and 2 new Meta lead ad variants for Turfli's top services: artificial turf installation in Scottsdale, backyard remodel in Scottsdale, and putting greens. Keep the brand voice premium and design-forward."
    else:
        user_msg = action  # Allow passing a custom prompt

    messages = [{"role": "user", "content": user_msg}]

    print(f"\n{'='*60}")
    print(f"TURFLI ADS AGENT | {today}")
    print(f"Platform: {platform.upper()} | Action: {action.upper()} | Dry Run: {dry_run}")
    print(f"{'='*60}\n")

    settings = config.get("settings", {})
    max_budget_increase_pct = settings.get("max_budget_increase_pct", 30)
    min_budget_usd = settings.get("min_budget_usd", 10)

    # Agentic loop
    while True:
        response = client_anthropic.messages.create(
            model="claude-opus-4-7",
            max_tokens=8096,
            system=SYSTEM_PROMPT + f"\n\nSafety constraints: Never increase any campaign budget by more than {max_budget_increase_pct}%. Never set any budget below ${min_budget_usd}/day.",
            tools=TOOLS,
            messages=messages,
        )

        # Print any text blocks
        for block in response.content:
            if hasattr(block, "text"):
                print(block.text)

        # Check stop reason
        if response.stop_reason == "end_turn":
            break
        elif response.stop_reason == "tool_use":
            # Execute tool calls
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    print(f"\n[Tool: {block.name}] {json.dumps(block.input, indent=2)[:200]}")
                    result = execute_tool(
                        block.name, block.input, config,
                        google_client=google_client,
                        meta_account=meta_account,
                        dry_run=dry_run,
                    )
                    print(f"[Result] {result[:300]}...")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            # Add assistant response and tool results to messages
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
        else:
            print(f"[STOP] {response.stop_reason}")
            break

    print(f"\n{'='*60}")
    print("Agent run complete.")
    print(f"{'='*60}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Turfli Ads Agent — Claude-powered Google & Meta Ads optimization")
    parser.add_argument("--platform", choices=["google", "meta", "both"], default="both",
                        help="Which ad platform to work with (default: both)")
    parser.add_argument("--action", default="analyze",
                        help="analyze | optimize | report | copy | or any custom prompt")
    parser.add_argument("--live", action="store_true",
                        help="Run in live mode (actually applies changes). Default is dry run.")
    parser.add_argument("--config", default="config.yml",
                        help="Path to config file (default: config.yml)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    run_agent(cfg, platform=args.platform, action=args.action, dry_run=not args.live)
