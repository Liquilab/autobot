---
name: eod
description: Einde-van-de-dag routine. Check bot health, portfolio, P&L, schrijf rapport.
---

Einde-van-de-dag routine voor de Polymarket trading bot. Doe dit in volgorde:

1. **Check bot health**: SSH naar de VPS (credentials in /Users/koen/autobot/vps_info.txt) en check:
   - `systemctl is-active autobot`
   - `tail -20 /var/log/autobot.log`
   - `tail -5 /var/log/autobot-error.log`

2. **Portfolio snapshot**: Haal de huidige posities en balans op:
   ```
   source venv/bin/activate && python3 -c "
   import requests
   FUNDER = '0x1240Ff4f31BF4e872d4700363Cc6EE2D11CCeec2'
   r = requests.get(f'https://data-api.polymarket.com/positions', params={'user': FUNDER})
   positions = r.json()
   total = sum(p.get('currentValue', 0) for p in positions if p.get('size', 0) > 0)
   print(f'Posities: {len([p for p in positions if p.get(\"size\", 0) > 0])}')
   print(f'Totale waarde: \${total:.2f}')
   for p in positions:
       if p.get('size', 0) > 0:
           print(f'  {p.get(\"title\", \"?\")[:50]}: {p[\"size\"]} {p.get(\"outcome\",\"?\")} @ \${p.get(\"curPrice\",0):.3f} = \${p.get(\"currentValue\",0):.2f}')
   "
   ```

3. **Check CLOB balance**:
   ```
   source venv/bin/activate && python3 -c "
   from src.execute_trades import get_client, get_balance
   print(f'Cash: \${get_balance(get_client()):.2f}')
   "
   ```

4. **Resolved trades vandaag**: Check of er trades resolved zijn en wat de P&L was

5. **Schrijf EOD rapport** naar reports/YYYY-MM-DD-eod.md in het Nederlands met:
   - Portfolio waarde en samenstelling
   - Trades van vandaag (aantal, winst/verlies)
   - Bot gezondheid
   - Strategie observaties
   - Plan voor morgen

6. **Save alles**: Voer /save uit om te committen, pushen, en syncen naar VPS

7. **Geef een kort Nederlands overzicht** aan de gebruiker
