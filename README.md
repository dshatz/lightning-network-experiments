## Setting up the environment

1. Install docker and docker-compose. [Instructions](https://docs.docker.com/compose/install/)
2. `docker-compose build && docker-compose up -d`

Both Bitcoin and cLightning nodes are going to be started.
Bitcoin is going to start fetching the blockchain into ./bitcoind-data. This will take a while and take 200-300GB of disk space.



## Why docker?
A docker composition is used for two main reasons:
 - Ability to create exactly the same environment on any machine. Dockerfiles take care of everything, from installing software of specific versions to configuring Tor connectivity and managing c-lightning plugins. 
 - Isolation from the host machine by means of docker virtualization. We choose to not expose bitcoind and \textit{lightning} nodes to the outside network without necessity. Moreover, they are also not exposed to the host machine, so even if RPC password gets compromised, an attacker will need access to the host machine to gain control of the bitcoin node.
