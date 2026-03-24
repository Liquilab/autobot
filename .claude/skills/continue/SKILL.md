---
name: continue
description: Hervat werk. Lees context, check bot, portfolio snapshot, actieplan.
---

Hervat het werk aan de Polymarket trading bot. Doe dit in volgorde:

1. **Lees context**:
   - Lees CLAUDE.md voor de missie en regels
   - Lees het meest recente rapport in reports/ (sorteer op datum)
   - Lees data/bot_state.json voor de huidige staat
   - Lees data/strategy_params.json voor de huidige strategie parameters

2. **Bot status**: SSH naar VPS (credentials in /Users/koen/autobot/vps_info.txt):
   - `systemctl is-active autobot`
   - `tail -20 /var/log/autobot.log`
   - Draait de bot? Zo nee, herstart.

3. **Portfolio snapshot**:
   - CLOB balance
   - Open posities via data API
   - Totale waarde vs startkapitaal ($100)

4. **Identificeer wat er te doen is**:
   - Zijn er bugs/errors in de logs?
   - Zijn er posities die handmatig aandacht nodig hebben?
   - Kan de strategie verbeterd worden op basis van recente resultaten?
   - Moet de code geupdate worden op de VPS?

5. **Geef een kort Nederlands overzicht** aan de gebruiker:
   - Hoe staat de bot ervoor
   - Wat is er gebeurd sinds de laatste sessie
   - Wat stel je voor om nu te doen

Stel GEEN vragen. Analyseer de situatie en stel een actieplan voor. De gebruiker beslist of hij het wil uitvoeren.
