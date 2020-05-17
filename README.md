## Setting up the environment

1. Install docker and docker-compose. [Instructions](https://docs.docker.com/compose/install/)
2. `docker-compose build && docker-compose up -d`

Both Bitcoin and cLightning nodes are going to be started.
Bitcoin is going to start fetching the blockchain into ./bitcoind-data. This will take a while and take 200-300GB of disk space.

