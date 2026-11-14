#!/bin/bash
set -e

TARGET="${1:-/home/unitree/robot-diy-card}"
mkdir -p "$TARGET"

cp g1_card_server.py "$TARGET"/
cp web_bridge.js "$TARGET"/
rm -rf "$TARGET/web"
cp -r web "$TARGET"/
cp robot-diy-card.service "$TARGET"/

sudo cp robot-diy-card.service /etc/systemd/system/robot-diy-card.service
sudo systemctl daemon-reload
sudo systemctl enable robot-diy-card
sudo systemctl restart robot-diy-card

echo "deployed to $TARGET"
echo "open http://$(hostname -I | awk '{print $1}'):8080/"
