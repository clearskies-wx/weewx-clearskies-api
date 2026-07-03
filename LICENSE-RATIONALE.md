# License Rationale

Clear Skies core repositories (API, Dashboard, Config UI) are licensed under the
**PolyForm Noncommercial License 1.0.0**. An accompanying
[ADDITIONAL-USES.md](ADDITIONAL-USES.md) extends the base license with specific
permitted uses for community organizations, family farms, amateur radio operators,
agricultural cooperatives, and tax-exempt organizations, and defines when a
separate commercial license is required.

## Why PolyForm Noncommercial?

The project was originally licensed under the GNU General Public License v3.0
(GPL v3) to align with the weewx ecosystem. However, GPL v3 permits unrestricted
commercial use — including advertising, paid subscriptions, managed hosting, and
white-labeling — which conflicts with the creator's intent. Clear Skies is free
for personal, educational, nonprofit, government, and community use. Commercial
use that generates revenue requires a paid license.

PolyForm Noncommercial 1.0.0 was chosen over alternatives because:

- **BSL 1.1 (Business Source License):** time-delayed open-source conversion
  doesn't match the project's model — there is no future date at which
  commercial use should become unrestricted.
- **Elastic License 2.0:** primarily designed for SaaS protection, overly
  complex for a self-hosted weather dashboard.
- **Custom license:** legal risk, unfamiliarity, and maintenance burden.
- **PolyForm NC 1.0.0:** plain-English, well-drafted by experienced licensing
  attorneys, already adopted by other projects, and directly addresses the
  noncommercial intent.

## Why not the weewx extensions?

The weewx extensions (`weewx-clearskies-extension` and `weewx-clearskies-truesun`)
remain licensed under **GPL v3**. They are derivative works of
[weewx](https://github.com/weewx/weewx), which is GPL v3 — the copyleft terms
require that extensions distributed alongside weewx carry the same license.

The core repos (API, Dashboard, Config UI) are independent works that do not
derive from weewx's source code. They read weewx's configuration and database
but contain no weewx code. This makes the license split legally clean.

## Decision record

See [ADR-081](../../docs/archive/decisions/ADR-081-license-change-polyform.md)
for the full decision record, including options considered, consequences, and
implementation guidance. ADR-081 supersedes [ADR-003](../../docs/archive/decisions/ADR-003-license.md).

## License files

- [LICENSE](LICENSE) — PolyForm Noncommercial License 1.0.0 (full text)
- [ADDITIONAL-USES.md](ADDITIONAL-USES.md) — Additional permitted uses and
  commercial requirements
