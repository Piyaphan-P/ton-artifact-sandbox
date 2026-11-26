#!/bin/sh
# Scoped to the Artifact runtime subnet. Does not modify existing Hermes rules.
set -eu
iptables -N ARTIFACT-EGRESS 2>/dev/null || true
iptables -F ARTIFACT-EGRESS
iptables -A ARTIFACT-EGRESS -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
for n in 1 2 3 4 5 6 7 8; do
  iptables -A ARTIFACT-EGRESS -s "172.29.$n.0/24" -d "172.29.$n.0/24" -j RETURN
done
for dest in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10 127.0.0.0/8 72.60.43.77/32; do
  iptables -A ARTIFACT-EGRESS -d "$dest" -j REJECT
done
iptables -A ARTIFACT-EGRESS -j RETURN
iptables -C DOCKER-USER -s 172.29.0.0/16 -j ARTIFACT-EGRESS 2>/dev/null || iptables -I DOCKER-USER 1 -s 172.29.0.0/16 -j ARTIFACT-EGRESS
iptables -C INPUT -s 172.29.0.0/16 -m conntrack --ctstate NEW -j REJECT 2>/dev/null || iptables -I INPUT 1 -s 172.29.0.0/16 -m conntrack --ctstate NEW -j REJECT
