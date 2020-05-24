#!/bin/sh

/etc/init.d/tor start
echo "$@"
exec "lightningd" $@