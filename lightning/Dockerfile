FROM elementsproject/lightningd:v0.8.2.1

RUN apt update && apt install -y python3 python3-pip

ADD plugins/ ./plugins/
RUN chmod a+x ./plugins/probe/probe.py
RUN pip3 install -r ./plugins/probe/requirements.txt

WORKDIR /
#CMD ["lightningd", "--network=bitcoin", "--log-level=debug"]
