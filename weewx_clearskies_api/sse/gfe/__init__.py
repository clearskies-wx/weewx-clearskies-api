"""NWS GFE Text Generation System with WorldCast Technology.

Ports the National Weather Service's Graphical Forecast Editor (GFE) text
formatter algorithms and threshold tables for use in the Clear Skies
forecast and current-conditions text engines (ADR-082).

Source: `Unidata/awips2` (https://github.com/Unidata/awips2), a public
domain US government work (17 USC S105). See
`docs/reference/nws-text-system/gfe-source-code-analysis.md` for the full
source analysis this package is ported from.

WorldCast refers to the i18n expansion of the GFE vocabulary beyond its
original French/Spanish support to 13 locales (not a legal brand).
"""
