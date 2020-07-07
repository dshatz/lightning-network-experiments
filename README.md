## Setting up the environment

1. Install docker and docker-compose. [Instructions](https://docs.docker.com/compose/install/)
2. `docker-compose build && docker-compose up -d`

Both Bitcoin and cLightning nodes are going to be started.
Bitcoin is going to start fetching the blockchain into ./bitcoind-data. This will take a while and take 200-300GB of disk space.

For ease of use you can add the following to the end of your `.bashrc`.
```
alias lightning-cli="docker exec -it research_lightning_1 lightning-cli"
```

## Why docker?
A docker composition is used for two main reasons:
 - Ability to create exactly the same environment on any machine. Dockerfiles take care of everything, from installing software of specific versions to configuring Tor connectivity and managing c-lightning plugins. 
 - Isolation from the host machine by means of docker virtualization. We choose to not expose bitcoind and \textit{lightning} nodes to the outside network without necessity. Moreover, they are also not exposed to the host machine, so even if RPC password gets compromised, an attacker will need access to the host machine to gain control of the bitcoin node.


## The `./results` folder and EIDs
The results folder is mounted in the docker container at `/root/results`. This is where the results are written to by default.
When starting a new experiment, a new experiment ID (EID) will be assigned to it. 

The `./results` folder will then contain the `<EID>.log` log file and some `<EID>-*.csv` CSV files.  

## Waiting for sync
First, make sure that both *Bitcoin* and *c-lightning* clients have synced with the network.

Run `docker-compose logs -f --tail=100 bitcoind` to view the log of the Bitcoin node. 
Run `lightning-cli getinfo` to see whether the network sync is completed.


## Connectivity experiment (`only_connect`)
The first experiments attempts to connect (via a peer to peer connection) to a list of nodes. Connection attempts are timed, errors are recorded.
After all connections have either succeeded or failed, the plugin waits 40 hours, periodically checking connection status.

### Preparation
First, we need to generate a list of nodes to connect to.
For that, you can use `lightning-cli listnodes > xxx.json` directly.
Alternatively, we provide a method to get a list of nodes based on their channel count.
```bash
lightning-cli nodes_with_degree <MIN_CHANNELCOUNT> <MAX_CHANNELCOUNT> <COUNT=50>
# Min and max boundaries are inclusive

# Get 25 nodes with just 1 to 5 channels. A file will be created: ./results/1-5.degreenodes (json formatted)
lightning-cli nodes_with_degree 1 5 25
```

### Running
The list of nodes has to be in a file and is passed to the experiment as the first parameter.
```bash
lightning-cli only_connect /root/results/nodes.json
```
The node list file has to be a json formatted file, in the same format as `lightning-cli listnodes`. If you want to run the experiment for all nodes, you can do the following:
```bash
lightning-cli listnodes > ./results/all-nodes.json
lightning-cli only_connect /root/results/all-nodes.json
```

Alternatively, running for 25 nodes with 1-5 channels:
```bash
lightning-cli nodes_with_degree 1 5 25
lightning-cli only_connect /root/results/1-5.degreenodes
```

If you want to use nodes from an 1ML REST endpoint, download the json and then apply the script in `util/1ml-to-listnodes.py` to it to get the right format.
After that, the converted file can be passed to `only_connect` in the same way as above.


The experiment will take a while to complete. You can track the progress:
```bash
tail -f ./results/<EID>.log
```

### Output
Apart from the `.log` file, 3 CSVs will be created:
 - `<EID>-peers.csv` will contain information about all connection attempts, timestamps and possible errors.
 - `<EID>-gossip.csv` will contain information about the size of the local network view.
 - `<EID>-disconnects.csv` (only if there were any disconnects) will contain peers that closed the connection and timestamps.


## Payment routing experiment (`probe_all`)
This experiment attempts to send 3 payment probes to each node in our local network view (i.e. that returned by `listnodes`).

### Preparation
Before starting with this experiment, at least one channel has to be created (and funded) with a hub node. 

A good idea is to establish a total of 3 channels with 3 hub nodes. You can find hub nodes on 1ml.com. 
Each channel must have large enough capacity to send many large payments at a high rate. 
From past experience, 22 USD is sufficient when sending payments of max 10 USD.

### Running
```bash
lightning-cli probe_all <PROGRESS_FILE=None> <PARALLEL_PROBES=3> <PROBE_COUNT_LIMIT=100000> 
```
**PROGRESS_FILE** - a path to a progress file, output by a previous execution of this experiment. 
Passing the progress file allows continuing from the same place an error occured. A progress file contains all probes already performed, so an experiment will not send those again.
Omitting a progress file executes all probes.

**PARALLEL_PROBES** - how many probes will be executed at the same time. Setting to one will send a probe only after the previous one has either succeeded or failed. 

**PROBE_COUNT_LIMIT** - limit how many probes to perform before exiting. Can be useful when testing.

A **probe** is a 'fake' payment, identified by destination node id and payment amount.
Probes will be retried up to 24 times in case of a temporary error. Getting a permanent error will stop all further attempts.

### Output
Apart from the `.log` file, 3 CSVs will be created:
 - `<EID>-no_route.csv` contains probes that target unreachable nodes. A probe is written to this file when the first call to `getroute` returns `No route...`.
 - `<EID>-payments.csv` contains information about all other probes (so, those that did not fail on the first attempt).
 Includes timestamps, number of attempts, success bool, node id, payment amount and fail codes.
 - `<EID>-progress.csv` contains all probes that were already completed (either with a success of a failure). This file can be optionally passed to `probe_all
 (explained in previous section).
 
## Network composition
The experiments described above do not produce much information about node properties and network composition.
To fix that, we provide a helper Python script.
1. `lightning-cli listnodes > nodes.json`
2. Modify the paths in `util/listnodes_to_csv.py`
3. Run the script. It will convert json to CSV, while also doing some data pre-processing.
4. You now have a CSV file with all nodes to import to your DBMS.
 
## Plotting
The reason experiments use CSV as their output format is that CSVs are easy to import into most DBMSs, as they have a relation structure.

After importing all CSVs into relational tables, many complex queries can be performed using joins.

We provide some scripts for plotting the data from a postgres database in `plotting/`. 
You may want to modify the queries or the charts a bit for your liking.

## Modifying the plugin
You can modify the plugin from outside docker, i.e. the file `./lightning/plugins/probe/probe.py`.
To reload the plugin without restarting `c-lightning`, run: 
```bash
sh reload-plugin.sh
```
*Reloading a plugin will immediately stop all running experiments.*

