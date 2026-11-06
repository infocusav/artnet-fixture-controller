# Installation guide

There are two ways to install the controller:

| | [Option A: SD card image](#option-a-sd-card-image) | [Option B: Existing Raspberry Pi OS](#option-b-existing-raspberry-pi-os) |
|---|---|---|
| **Best for** | The quickest setup | Choosing your own login, and SSH from the start |
| **Login account** | None by default | Yours |
| **Network settings** | Already applied | Applied with one option |

Option B is how the original Pi 4 unit was set up. Images are built automatically from the latest Raspberry Pi OS Lite for each release, so check a new image on your hardware before relying on it.

## Network addresses

| Port | Job | Address |
|---|---|---|
| **Built-in Ethernet (`eth0`)** | Art-Net output | `2.0.0.100`, mask `255.0.0.0` |
| **USB Ethernet adapter (`eth1`)** | Control page and SSH | `192.168.1.100`, mask `255.255.255.0` |

Neither has a gateway, as the controller doesn't need internet access. Keep the two ports on separate switches or cables.

## Option A: SD card image

1. Download `artnet-fixture-controller-<version>.img.xz` from the [latest release](../../../releases/latest). There's no need to unpack it.
2. In [Raspberry Pi Imager](https://www.raspberrypi.com/software/), choose your Pi model, then **Use custom** under the operating system, and select the file. balenaEtcher also works.
3. Write it to the card, put the card in the Pi and connect the network.
4. Power on and wait about 3 minutes, as the first boot expands the card and restarts.
5. Set your computer up as described under [Connect your computer](#connect-your-computer), then open the control page.

**About logging in:** the image has no default password, and the controller runs without an account. To log in for maintenance, use Imager's OS customisation to set a username, password and SSH if it offers this for your image. Otherwise, connect a keyboard and screen, create an account when prompted, and turn on SSH:

```bash
sudo systemctl enable --now ssh
```

## Option B: Existing Raspberry Pi OS

1. Using Raspberry Pi Imager, write **Raspberry Pi OS Lite (64-bit)** to the card. In the OS customisation settings, set a username and password, and enable SSH with password authentication.
2. Start the Pi and log in over SSH.
3. Download the source code zip from the [latest release](../../../releases/latest) and copy it to the Pi. From a Windows computer:
   ```bat
   scp artnet-fixture-controller.zip yourusername@<pi-address>:~/
   ```
4. Unpack and install it on the Pi. Your folder name may include a version number:
   ```bash
   unzip artnet-fixture-controller.zip
   cd artnet-fixture-controller
   chmod +x install.sh scripts/*.sh
   sudo ./install.sh --network
   ```

`--network` applies the addresses above, and your SSH session may drop as they change. Leave it out to keep the Pi's current network settings. You'd then set **Send to IP address** to suit your own network.

## Connect your computer

Your computer needs an address in the same range as the Pi's port.

| Connecting to | Example computer address | Control page |
|---|---|---|
| **Control network** | `192.168.1.10`, mask `255.255.255.0` | `http://192.168.1.100:8080` |
| **Art-Net network** | `2.0.0.199`, mask `255.0.0.0` | `http://2.0.0.100:8080` |

On Windows, run this in **Command Prompt as Administrator**, using your adapter's name from `ipconfig`:

```bat
netsh interface ipv4 add address "Ethernet" 192.168.1.10 255.255.255.0
```

## Set up Art-Net

1. Connect the Pi's built-in port and your Art-Net to DMX adapter to the same switch or cable.
2. Open **Art-Net setup** on the control page.
3. Set **Send to IP address** to `2.255.255.255`, which reaches every device in the `2.x.x.x` range.
4. Set **Universe** to match the adapter's output. The number is Net × 256 + Sub-Net × 16 + Universe, so all zeros gives `0`.
5. Choose the **Fixture channel layout**, such as `RGB` or `DRGB`, to match each fixture's DMX mode. In layouts with `D`, the dimmer channel is held at full and Master does the dimming. In layouts with `W`, white stays off.
6. Set each fixture's **Start address**, then select **Save Art-Net setup**.

Press **GO**. The adapter's DMX light should come on and the fixtures should respond.

## Check it's running

If you have a login:

```bash
systemctl is-active fixture-control
ip -br addr
journalctl -u fixture-control -n 20 --no-pager
```

To test recovery, unplug the Pi for 10 seconds, plug it back in and wait about a minute. The cues or effect should restart by themselves.

## Back up your show

Cues and settings are saved in `/var/lib/fixture-control/show.json`. To copy them to your computer, first run this on the Pi:

```bash
sudo cp /var/lib/fixture-control/show.json ~/show-backup.json
sudo chown "$USER" ~/show-backup.json
```

Then run this on your computer:

```bash
scp yourusername@192.168.1.100:~/show-backup.json .
```

To restore a backup, stop the service, copy the file back over `show.json`, then start the service. Run `sudo chown --reference=/var/lib/fixture-control /var/lib/fixture-control/show.json` before starting it.

## Troubleshooting

| What you see | Likely cause | What to do |
|---|---|---|
| `Reply from <your Wi-Fi address>: Destination host unreachable` | Your Wi-Fi uses the same range, so traffic is going out over Wi-Fi | Turn off Wi-Fi, or give the wired adapter a lower metric: `netsh interface ipv4 set interface "Ethernet" metric=5` |
| The page won't load, and the ping times out | Wrong cable or port, or the Pi is still starting | Check the link lights and that your computer's address is in the range for that port |
| The Pi appears in ArtNetominator, but with no data | Wrong broadcast address or mask | Use `2.255.255.255` with mask `255.0.0.0`. `2.0.0.255` isn't a broadcast address on this network |
| Data appears in Wireshark, but not ArtNetominator | Your computer's mask or firewall | Check the mask matches the Pi's, and allow the tool through the firewall |
| Data is on the network, but there's no DMX | Adapter's universe or direction | Check the universe matches, and that the adapter has a DMX output |
| Fixtures show the wrong colours or respond oddly | Channel layout or DMX address | Match the layout to the fixture's mode, and check its address |
| `Address already in use` when running the program by hand | The service is already running | Stop it first: `sudo systemctl stop fixture-control` |
| The USB adapter shows `DOWN` | No live cable on it | Connect it to a powered switch or a computer |
| The red bar says Art-Net couldn't be sent | The Pi has no route to that address | Check the built-in port's cable, and that the address is in the range `2.x.x.x` |

**Note:** port names are set at boot. To confirm `eth0` is the built-in port, run `readlink -f /sys/class/net/eth0/device`. The path shouldn't contain `usb`.
