# Pywikibot settings for the NVA bot. Copy to user-config.py (do not commit it).
family = "wikidata"
mylang = "wikidata"
usernames["wikidata"]["wikidata"] = "NVAImportBot"
usernames["wikidata"]["test"] = "NVAImportBot"   # for trial runs on test.wikidata.org
password_file = "user-password.py"                # holds the BotPassword, see README

write_maxlag = 5      # back off when the servers are lagged (Pywikibot >= 11.8; older: maxlag = 5)
put_throttle = 6      # seconds between edits; lower only after approval
user_agent_description = "NVAImportBot (https://www.wikidata.org/wiki/User:NVAImportBot)"
