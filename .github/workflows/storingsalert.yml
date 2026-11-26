name: Storingsalert trein Arriva

on:
  schedule:
    - cron: "*/5 * * * *"   # reserve; de wekker bij cron-job.org start de runs elke 5 minuten
  workflow_dispatch:         # handmatig starten via tabblad Actions (en door de wekker)
    inputs:
      testmodus:
        description: "Testbericht naar Teams sturen?"
        type: choice
        options: ["nee", "ja"]
        default: "nee"

permissions:
  contents: write            # nodig om data/state.json en data/logboek.csv op te slaan

concurrency:
  group: storingsalert
  cancel-in-progress: false

jobs:
  controle:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - name: Code ophalen (laatste stand)
        uses: actions/checkout@v5
        with:
          ref: main          # altijd de nieuwste status, niet de stand van het startmoment

      - name: Verstoringen controleren
        env:
          NS_API_KEY: ${{ secrets.NS_API_KEY }}
          TEAMS_WEBHOOK_URL: ${{ secrets.TEAMS_WEBHOOK_URL }}
          TEAMS_WEBHOOK_NOORD: ${{ secrets.TEAMS_WEBHOOK_NOORD }}
          TEAMS_WEBHOOK_OOST: ${{ secrets.TEAMS_WEBHOOK_OOST }}
          TEAMS_WEBHOOK_LIMBURG: ${{ secrets.TEAMS_WEBHOOK_LIMBURG }}
          TEST_MODE: ${{ github.event.inputs.testmodus == 'ja' && '1' || '0' }}
        run: python3 storingsalert.py

      - name: Status en logboek opslaan
        # ook als een regiokanaal faalde (dan is de status wel bijgewerkt)
        if: ${{ !cancelled() && github.event.inputs.testmodus != 'ja' }}
        run: |
          git config user.name "storingsalert-bot"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add data/
          if git diff --cached --quiet; then
            echo "Geen wijzigingen"
            exit 0
          fi
          git commit -m "Status bijgewerkt"
          for poging in 1 2 3; do
            git push && exit 0
            echo "Opslaan botste, opnieuw proberen ($poging)"
            git pull --rebase -X theirs origin main
          done
          exit 1
