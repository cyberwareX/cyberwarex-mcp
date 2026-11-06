"""Agent Web-Access — MCP server (stdio).

Exposes the web-access capabilities as MCP tools so any MCP-speaking agent
(Claude Desktop, Bedrock AgentCore, Cursor, etc.) can call them:

    web_fetch(url, format)       -> rendered page as markdown/text/html
    web_extract(url, selector)   -> elements matching a CSS selector
    web_screenshot(url, ...)     -> PNG (base64)
    web_pdf(url)                 -> PDF (base64)
    telegram_search(q)           -> public Telegram channels, groups and bots for a topic
    telegram_lookup(name, ...)   -> one public @name: type, members, bio, latest posts
    telegram_check(name)         -> alive, quiet, dead or looks inflated
    social_profile(...)          -> public X, TikTok, Instagram, Facebook or LinkedIn profile
    company_profile(domain)      -> company card from RDAP, DNS and the homepage

It is a thin, stateless proxy to the HTTP service (default https://web.cyberwarex.com,
override with AWA_BASE_URL — set this to the public funnel URL for remote agents).

Payment model: the HTTP service is x402-gated. This MCP server forwards an
`X-PAYMENT` header when the caller provides one via the AWA_X_PAYMENT env var, and
otherwise surfaces the 402 payment requirement (the full `accepts` + `extensions.bazaar`
block) back to the agent so *its* x402 client can pay and retry. That keeps payment
on the calling agent's wallet — this server never holds keys. For unattended use,
set AWA_RAPIDAPI_SECRET (bypass) or run the service in placeholder mode.

Run:  python mcp_server.py       (speaks MCP over stdio)
"""
from __future__ import annotations

import asyncio
import json
import os

import requests
from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

BASE_URL = (os.environ.get("AWA_BASE_URL") or "https://web.cyberwarex.com").rstrip("/")
X_PAYMENT = os.environ.get("AWA_X_PAYMENT", "").strip()
RAPIDAPI_SECRET = os.environ.get("AWA_RAPIDAPI_SECRET", "").strip()
HTTP_TIMEOUT = float(os.environ.get("AWA_TIMEOUT", "60"))

def _headers() -> dict:
    h = {"Accept": "application/json",
         # Cloudflare 403s some default library UAs on our API hosts — always identify.
         "User-Agent": "cyberwarex-mcp/1.0 (+https://cyberwarex.com)"}
    if X_PAYMENT:
        h["X-PAYMENT"] = X_PAYMENT
    else:
        # No payment configured -> opt into the service free trial so an evaluator\'s
        # FIRST calls return REAL DATA instead of a paywall. Without this the very first
        # MCP tool call an evaluator makes returns a 402 and they never see the product.
        h["X-Free-Trial"] = "1"
    if RAPIDAPI_SECRET:
        h["X-RapidAPI-Proxy-Secret"] = RAPIDAPI_SECRET
    return h


def _call(path: str, params: dict) -> str:
    """Call the HTTP service; return a text payload for the agent.

    On 402, return the payment requirement (so the agent's x402 client can pay).
    """
    try:
        r = requests.get(f"{BASE_URL}{path}", params=params, headers=_headers(),
                         timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        return json.dumps({"error": "request failed", "reason": str(e)})
    if r.status_code == 402:
        try:
            body = r.json()
        except ValueError:
            body = {"error": "payment required"}
        return json.dumps({
            "x402_payment_required": True,
            "hint": "Pay the x402 invoice below (USDC on Base) and retry with an "
                    "X-PAYMENT header, or set AWA_X_PAYMENT for this MCP server.",
            "invoice": body,
        }, indent=2)
    try:
        return json.dumps(r.json(), indent=2)
    except ValueError:
        return r.text


TOOLS = [
    types.Tool(
        name="web_fetch",
        description="Read any public web page, JavaScript sites included, as clean "
                    "markdown, text or html. $0.001 a page.",
        inputSchema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Public http(s) URL to render"},
                "format": {"type": "string", "enum": ["markdown", "text", "html"],
                           "default": "markdown"},
            },
            "required": ["url"],
        },
    ),
    types.Tool(
        name="web_extract",
        description="Pull just the parts you need from a rendered page: text and "
                    "attributes of every element matching a CSS selector. $0.001.",
        inputSchema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "selector": {"type": "string", "description": "CSS selector"},
            },
            "required": ["url", "selector"],
        },
    ),
    types.Tool(
        name="web_screenshot",
        description="Render a live web page and return a PNG screenshot (base64).",
        inputSchema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "fullpage": {"type": "boolean", "default": False},
                "width": {"type": "integer", "default": 1280},
            },
            "required": ["url"],
        },
    ),
    types.Tool(
        name="web_pdf",
        description="Render a live web page and return it as a PDF (base64).",
        inputSchema={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    ),
    types.Tool(
        name="telegram_search",
        description="Find public Telegram channels, groups and bots for a topic, with "
                    "member counts and links. No Telegram account needed. $0.01 a search.",
        inputSchema={
            "type": "object",
            "properties": {"q": {"type": "string",
                                 "description": "Topic words, e.g. solana developers"}},
            "required": ["q"],
        },
    ),
    types.Tool(
        name="telegram_lookup",
        description="Look up one public Telegram @name: channel, group, bot or user, "
                    "members, bio, and optionally its latest public posts or a word "
                    "search inside them. $0.005.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Public @name or https://t.me/name"},
                "posts": {"type": "boolean", "default": False,
                          "description": "Also return the latest public channel posts"},
                "q": {"type": "string", "description": "Search words inside the channel's public posts"},
            },
            "required": ["name"],
        },
    ),
    types.Tool(
        name="telegram_check",
        description="Before you join, post in or pay a Telegram channel: is it alive, "
                    "quiet, dead or does it look inflated, from its public numbers. $0.01.",
        inputSchema={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Public @name or https://t.me/name"}},
            "required": ["name"],
        },
    ),
    types.Tool(
        name="social_profile",
        description="Public profile fields (name, bio, followers, links) from an X, TikTok, "
                    "Instagram, Facebook or LinkedIn page, logged out. Give a url, or a "
                    "handle or name plus platform. $0.005.",
        inputSchema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Profile link, e.g. https://x.com/vercel"},
                "handle": {"type": "string", "description": "Profile name instead of url (needs platform)"},
                "q": {"type": "string", "description": "Person or brand name to find by search (needs platform)"},
                "platform": {"type": "string",
                             "enum": ["x", "tiktok", "instagram", "facebook", "linkedin"]},
            },
        },
    ),
    types.Tool(
        name="company_profile",
        description="Who is behind a domain: company card from public RDAP, DNS and the "
                    "homepage (owner, age, mail setup, socials). $0.005.",
        inputSchema={
            "type": "object",
            "properties": {"domain": {"type": "string", "description": "Domain or URL, e.g. stripe.com"}},
            "required": ["domain"],
        },
    ),
]


def _dispatch(name: str, args: dict) -> str:
    args = args or {}
    if name == "web_fetch":
        return _call("/fetch", {"url": args.get("url"),
                                "format": args.get("format", "markdown")})
    if name == "web_extract":
        return _call("/extract", {"url": args.get("url"),
                                  "selector": args.get("selector")})
    if name == "web_screenshot":
        return _call("/screenshot", {"url": args.get("url"),
                                     "fullpage": str(args.get("fullpage", False)).lower(),
                                     "width": args.get("width", 1280)})
    if name == "web_pdf":
        return _call("/pdf", {"url": args.get("url")})
    if name == "telegram_search":
        return _call("/telegram/search", {"q": args.get("q")})
    if name == "telegram_lookup":
        params = {"name": args.get("name")}
        if args.get("posts"):
            params["posts"] = "1"
        if args.get("q"):
            params["q"] = args.get("q")
        return _call("/telegram", params)
    if name == "telegram_check":
        return _call("/telegram/check", {"name": args.get("name")})
    if name == "social_profile":
        return _call("/social", {k: args.get(k) for k in ("url", "handle", "q", "platform")
                                 if args.get(k)})
    if name == "company_profile":
        return _call("/company", {"domain": args.get("domain")})
    return json.dumps({"error": f"unknown tool: {name}"})


async def on_list_tools(ctx, params) -> types.ListToolsResult:
    return types.ListToolsResult(tools=TOOLS)


async def on_call_tool(ctx, params) -> types.CallToolResult:
    out = await asyncio.to_thread(_dispatch, params.name, params.arguments or {})
    return types.CallToolResult(content=[types.TextContent(type="text", text=out)])


server = Server(
    "agent-web-access",
    on_list_tools=on_list_tools,
    on_call_tool=on_call_tool,
)


async def _main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_main())
