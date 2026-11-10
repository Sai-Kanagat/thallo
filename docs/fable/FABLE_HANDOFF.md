# Handoff for the architecture brief

Facts and constraints only.

- **Builder:** one person, designer turned AI builder, building with AI coding agents. Limited AI usage budget.
- **Buyer scenario:** a SaaS or service company with about 20 employees and a 5-person marketing team.
- **Offer:** software only, EUR 2,000 a month. The client keeps one marketer as reviewer and operator. Covers marketing and sales support up to booked meetings. People still close deals.
- **Targets:** reviewer time kept low; error or failure rate under 5%; lowest possible cost per lead; running cost per client far below the price; ad spend is separate.
- **Regions:** India, Italy and others. Law per region is out of scope here, but the architecture must support per-region rules.
- **Trust model:** everything starts manual. Task types earn autonomy as accuracy is proven, and drop back to manual on failure.
- **Hard rules:** nothing published, sent or spent without approval tied to the exact content; no fabricated claims; no fake engagement or personas; platform terms respected.
- **Tools available, use or reject on merit:** Notion, n8n, Postiz (scheduler with an analytics API), HubSpot free, GA4, Search Console, Claude and Gemini models, Open Higgsfield AI (open-source image, video and lip-sync studio; runs through paid Muapi.ai credits; licence unverified), raea (React and TypeScript to video on a local GPU; FSL licence; early-stage).
- **Existing parts, reuse only if they fit:** an approval gate tied to content hashes; a Notion calendar adapter; a Postiz adapter; a check that blocks unsourced numbers; sealed forecasts scored after publishing; a HubSpot setup script; one real business with consented data for a live pilot.
