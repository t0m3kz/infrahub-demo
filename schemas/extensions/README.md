# Schema extentions

## 🧩 BGP

This schema extension contains all you need to model your BGP platform

- Nodes
  - AutonomousSystem
  - BGPPeerGroup
  - BGPSession

- Dependencies
  - Base

## 🧩 Circuit

This schema extension contains Circuits and ways to connect them with your infrastructure!

- Nodes
  - Circuit

- Dependencies
  - Base

## 🧩 VRF

This schema extension contains models to support VRF in your network.

- Nodes
  - VRF
  - RouteTarget

- Dependencies
  - Base

## 🧩 VLAN

This schema extension contains models to support VLANs in you network.

- Nodes
  - Vlan
  - L2Domain

- Dependencies
  - Base

## 🧩 SFP

This schema extension gives you all the models you need to document Small Form-factor Pluggable (SFP).

You can either plug it into an interface or attach it to a location (e.g. it's a spare SFP stored in a rack).

Improvements:

- As of now there is no verification with type / form factor / protocol / distance ...
- You could plug any SFP into any equipment interface (e.g. a virtual interface ...)
- You could link a SFP to an interface AND a location ...
- Maybe add a link toward manufacturer ...

- Nodes
  - SFP

- Dependencies
  - Base

## 🧩 Location

This schema extension will provide you with basic items to store location related data.

- Nodes
  - Region
  - Country
  - Metro
  - Building
  - Floor
  - Suite
  - Rack

- Dependencies
  - Base


## 🧩 Security

This schema extension is minimal but will provide you with basic items to store location related data.

Namespace : Security

- Generics
  - Policy
  - Address Group
  - Address
  - Service Group
  - Service

- Nodes
  - Zone
  - IPAM IP Address
  - IPAM IP Prefix
  - IP Address
  - Prefix
  - IP Range
  - FQDN
  - Address Group
  - IP Protocols
  - Service
  - Service range
  - Service group
  - Security Policy
  - Policy rule

- Nodes in another namespace
  - Frewall (Dcim)

- Dependencies
  - Base
