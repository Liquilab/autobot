---
name: continue
description: Hervat werk. Lees context, check bot, portfolio snapshot, actieplan.
---

Hervat het werk aan de Polymarket trading bot. Doe dit in volgorde:

1. **Sync van VPS** (credentials in /Users/koen/autobot/vps_info.txt):
   - Sync logboek: `scp root@VPS:/opt/autobot/reports/logboek.md /Users/koen/autobot/reports/logboek.md`
   - Sync data: `scp root@VPS:/opt/autobot/data/*.json /Users/koen/autobot/data/`
   - Sync reports: `scp root@VPS:/opt/autobot/reports/research/*.md /Users/koen/autobot/reports/research/`

2. **Lees context**:
   - Lees CLAUDE.md voor de missie en regels
   - Lees reports/logboek.md (laatste 2 coach sessies) voor wat de AI coach recent adviseerde
   - Lees data/strategy_params.json voor de huidige (door coach aangestuurde) parameters

3. **Bot status**: SSH naar VPS:
   - `systemctl is-active autobot`
   - `tail -30 /var/log/autobot.log`
   - Draait de bot? Zo nee, herstart.

4. **Portfolio snapshot** (gebruik LIVE API data, NOOIT gokken):
   - Haal actuele crypto prijzen op via Binance API
   - CLOB cash balance
   - Open posities waarde via Data API
   - Totale portfolio waarde (cash + posities)

5. **Identificeer wat er te doen is**:
   - Zijn er bugs/errors in de logs?
   - Wat heeft de coach geadviseerd? Worden die adviezen ook daadwerkelijk uitgevoerd?
   - Zijn er posities die aandacht nodig hebben?
   - Moet de code geupdate worden op de VPS?

6. **Geef een kort Nederlands overzicht** aan de gebruiker:
   - Portfolio waarde en P&L
   - Wat de coach recent adviseerde
   - Wat er goed/fout gaat
   - Wat je voorstelt om nu te doen

Stel GEEN vragen. Analyseer de situatie en stel een actieplan voor. De gebruiker beslist of hij het wil uitvoeren.
