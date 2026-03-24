---
name: morning
description: Ochtend briefing. Overnight resultaten, portfolio, markt scan, plan.
---

Ochtend routine voor de Polymarket trading bot. Doe dit in volgorde:

1. **Bot status**: SSH naar VPS (credentials in /Users/koen/autobot/vps_info.txt):
   - Is de bot nog actief? `systemctl is-active autobot`
   - Laatste 30 log regels
   - Errors overnight?

2. **Overnight resultaten**: Check welke markten resolved zijn sinds gisteren:
   - Haal posities op via data API
   - Vergelijk met data/positions.json voor resolved trades
   - Bereken overnight P&L

3. **Portfolio overzicht**:
   - Huidige cash balance (CLOB)
   - Alle open posities met huidige waarden
   - Totale portfolio waarde vs startkapitaal ($100)
   - Welke posities resolven vandaag?

4. **Markt scan**: Welke markten zijn interessant voor vandaag?
   - Sports events vandaag (NBA, NHL, tennis)
   - Kort-termijn events die bijna resolven
   - Zijn er signaal-divergenties gevonden door de bot?

5. **Research loop check**: Heeft de bot geleerd overnight?
   - Check data/strategy_params.json voor wijzigingen
   - Check reports/research/ voor nieuwe research notes

6. **Schrijf morning rapport** naar reports/YYYY-MM-DD-morning.md in het Nederlands

7. **Geef een kort Nederlands briefing** aan de gebruiker: wat is er gebeurd, hoe staan we ervoor, wat is het plan
