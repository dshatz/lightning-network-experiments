docker exec -it research_lightning_1 lightning-cli plugin stop probe.py
docker exec -it research_lightning_1 lightning-cli plugin start /plugins/probe/probe.py
