FROM elementsproject/lightningd:v0.8.2.1

RUN apt update && apt install -y python3 python3-pip apt-transport-https curl

# Install tor service
RUN echo "deb https://deb.torproject.org/torproject.org stretch main" >> /etc/apt/sources.list
RUN curl https://deb.torproject.org/torproject.org/A3C4F0F979CAA22CDBA8F512EE8CBC9E886DDD89.asc | gpg --import
RUN gpg --export A3C4F0F979CAA22CDBA8F512EE8CBC9E886DDD89 | apt-key add -
RUN apt update
RUN apt install -y tor

# Configure tor service
RUN echo ControlPort 9051 >> /etc/tor/torrc
RUN echo CookieAuthentication 1 >> /etc/tor/torrc
RUN echo CookieAuthFileGroupReadable 1 >> /etc/tor/torrc
RUN echo SocksPort 9050 >> /etc/tor/torrc
RUN echo "SOCKSPolicy accept *" >> /etc/tor/torrc


ADD plugins/probe/requirements.txt ./plugins/probe/
RUN pip3 install -r ./plugins/probe/requirements.txt

ADD entrypoint.sh /
CMD ["lightningd"]
ENTRYPOINT ["/entrypoint.sh"]
WORKDIR /
