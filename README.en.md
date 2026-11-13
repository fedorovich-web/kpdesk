# KPDesk

[Русский](README.md) | **English**

A workspace for preparing sales quotations from customer requests. KPDesk helps
review requirements, select products from a company's price list, calculate
costs and create a PDF after a manager checks the result.

[Project website](https://kpdesk.ru) · [Demo](https://kpdesk.ru/demo) · [Workspace](https://kpdesk.ru/app)

## The problem

Supplier sales teams often copy product lists from customer emails into
spreadsheets, search price lists, compare specifications and format each offer
by hand. A change to the request means checking the selection and totals again.

KPDesk keeps the request, product selection and quotation together. Prices come
from the company's catalogue; the manager reviews the final products and text.

## Features

| Task | What the workspace supports |
| --- | --- |
| Import a catalogue | Upload CSV or XLSX price lists with SKUs, prices, sales units and specifications. |
| Review a request | Use an AI draft or enter items manually, checking quantities, units and source quotations. |
| Select products | Compare catalogue candidates against requirements and explicitly choose suitable items. |
| Calculate costs | Account for quantities, compatible units, price bases and sales increments. |
| Add company details | Look up both parties by Russian taxpayer ID through DaData and review the returned details. |
| Prepare a document | Review the text and terms, save a version and download a PDF with products, totals and company details. |
| Revisit work | Open saved quotations and their version history. |
| Work as a team | Keep organizations' data separate and enforce participant permissions. |

Missing or conflicting information requires clarification. AI does not set
prices or approve a quotation. PDFs can span multiple pages as needed.

## Try it

1. Open the [demo](https://kpdesk.ru/demo) to explore the flow with sample data.
2. Upload a company price list and add a customer request in the workspace.
3. Review requirements and product choices, then add company details and terms.
4. Save the quotation and download its PDF.

The demo requires no registration and makes no external AI calls.

## Engineering choices

- **Validated inputs.** AI output is checked for structure and consistency with the source request.
- **Separate pricing.** Prices come from the catalogue; money is calculated using Decimal.
- **Versioned documents.** Products, terms and company details belong to a saved version. Later profile edits do not rewrite old quotations.
- **Server-side access control.** Data and actions are scoped to organizations and participant roles.
- **Accountable text generation.** AI operations are tracked; failed or cancelled attempts are not reported as successful responses.

## Stack

| Component | Technologies | Source |
| --- | --- | --- |
| Website and workspace | TypeScript, React, Vite | [frontend](frontend) |
| API and business logic | Python, FastAPI, Pydantic | [backend/src/kpdesk](backend/src/kpdesk) |
| Data | PostgreSQL, SQLAlchemy, Alembic | [backend/migrations](backend/migrations) |
| Selection and pricing | Compatibility rules, lexical ranking, Decimal | [universal](backend/src/kpdesk/universal) |
| PDF | ReportLab | [quotes](backend/src/kpdesk/quotes) |

The application uses Docker. Dependency versions are locked in
`frontend/package-lock.json` and `backend/uv.lock`.

## Quality checks

pytest and Vitest cover domain logic and interface behavior. Checks address
pricing, version persistence, access control, catalogue imports, AI response
handling and PDF content. The project also uses type checking, linters,
production builds and browser scenarios.

## Author and use of the code

A project by [fedorovich-web](https://github.com/fedorovich-web).

The source is available for reviewing the implementation. No license for free
use or redistribution is granted; reuse requires the author's permission.
