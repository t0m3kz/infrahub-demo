# Infrahub Demo - Use Cases

Complete demonstrations of design-driven network automation using Infrahub. Each demo is self-contained with its own deployment script and documentation.

## 🚀 Quick Start

Choose a demo and run its deployment script:

```bash
cd demos/{demo_name}
./deploy.sh
```

## Available Demos

### 1. 🖖 [Data Center Topology Generation](./data_center_topology/)

**Captain Picard's Prime Directive**

Deploy 5 different data center architectures (DC-1 through DC-5) with a single command. Like Captain Jean-Luc Picard commanding the USS Enterprise with unwavering precision, this demo builds your entire infrastructure topology from scratch.

```bash
cd data_center_topology
./deploy.sh --scenario dc3
```

**Creates:** TopologyDataCenter, LocationBuilding, IpamPrefix

---

### 2. ⚡ [Pod & Rack Generation](./pod_rack_generation/)

**The Force Awakens with Automation**

Channel your inner Jedi Master Obi-Wan Kenobi and let the Force flow through your infrastructure! This demo creates hierarchical pod structures and rack layouts with the precision of Force-wielding Jedi.

**⚠️ Uses DC-1 (Paris)** – Adds new pods to existing DC-1 topology

```bash
cd data_center_topology
./deploy.sh --scenario dc1

cd ../pod_rack_generation
./deploy.sh
```

**Creates:** TopologyDataCenterPod, TopologyRack, DcimPhysicalDevice (switches)

---

### 3. 🔌 [Server Connectivity Cabling](./server_connectivity/)

**Scotty's Engineering Miracles**

*"I'm giving her all she's got, Captain!"* – Transport layer engineers, beam down those server connections! This demo ensures every physical server gets connected with dual-uplink redundancy.

**⚠️ Uses DC-1 (Paris)** – Deploys servers to existing DC-1 topology

```bash
cd data_center_topology
./deploy.sh --scenario dc1

cd ../server_connectivity
./deploy.sh
```

**Creates:** DcimPhysicalDevice (servers), DcimInterface, DcimCable

---

### 4. 📊 [Configuration Generation & Templating](./configuration_generation/)

**Borg Hive Collective Perfection**

*"Resistance is futile. All devices will be assimilated... to configuration."* – Just like the Borg Collective's unified consciousness, every device receives identical, perfectly templated configurations.

```bash
cd configuration_generation
./deploy.sh --scenario dc3
```

**Creates:** ArtifactDefinition, ConfigFile (device configurations)

**Supports:** 7 different device platforms with Jinja2 templating

---

### 5. ✅ [Validation & Health Checks](./validation_checks/)

**Morpheus's Training Program Verification**

*"There is no bug in your network."* – Like Morpheus testing Neo in the training program, our validation checks ensure your network topology is production-ready.

```bash
cd validation_checks
./deploy.sh --scenario dc3
```

**Creates:** CheckDefinition, ValidationResult

**Coverage:** 5 comprehensive validation checks across topology and configurations

---

### 6. 🚦 [Network Design as Code](./network_design/)

**Ada Lovelace's Divine Algorithm**

This is the original "design-driven automation"! Before it was cool, Ada Lovelace envisioned machines following perfect designs. Your network schemas define the divine algorithm.

```bash
cd network_design
./deploy.sh
```

**Creates:** 20+ custom schema nodes with inheritance from base DCIM/IPAM

---

### 7. 📡 [Equinix POP (Point of Presence)](./equinix_pop/)

**Federation's Interstellar Communication Array**

Deploy distributed Points of Presence across multiple locations like the Federation's array of space stations. Each POP is a gateway to the galaxy.

```bash
cd equinix_pop
./deploy.sh --scenario pop
```

**Creates:** TopologyPOP, LocationBuilding, EquinixFacility, DcimPhysicalDevice (peering routers)

**Optional:** `uv run invoke clab up --pop` to simulate locally

---

## Prerequisites

Before running any demo:

1. **Start Infrahub**

   ```bash
   cd ..
   uv run invoke start
   ```

2. **Set environment variables** (optional)

   ```bash
   export INFRAHUB_ADDRESS="http://localhost:8000"
   export INFRAHUB_API_TOKEN="06438eb2-8019-4776-878c-0941b1f1d1ec"
   ```

3. **Load initial setup** (from root directory)

   ```bash
   uv run infrahubctl schema load schemas
   uv run infrahubctl menu load menu
   ```

## Demo Execution Flow

Each demo follows this pattern:

1. **Verification** - Checks if Infrahub is running
2. **Data Loading** - Loads demo-specific configurations
3. **Generation** - Runs generators or transformations
4. **Validation** - Verifies results
5. **Reporting** - Shows generated objects and access points

## Common Flags

Most demos support these flags:

- `--scenario {name}` - Specify which scenario to deploy
- `--with-servers` - Include server connectivity
- `--with-security` - Add security configurations
- `--with-lb` - Deploy load balancers
- `--branch {name}` - Deploy to specific branch

Example:

```bash
./data_center_topology/deploy.sh --scenario dc3 --with-servers --with-security
```

## Data Organization

Each demo has its own `data/` folder:

```text
demos/
├── data_center_topology/data/
├── pod_rack_generation/data/
├── server_connectivity/data/
├── configuration_generation/data/
├── validation_checks/data/
├── network_design/data/
└── equinix_pop/data/
```

Place demo-specific configurations, templates, and definitions in these folders.

## Accessing Results

After running a demo, access results in Infrahub UI:

- **Topology Objects** - View → Infrastructure → Topology
- **Devices** - View → Infrastructure → Devices
- **Configurations** - Services → Artifacts
- **Health Checks** - Services → Health Checks
- **Schemas** - Admin → Schemas

## Cleanup

To remove all objects from a demo:

```bash
# Delete the branch created during deployment
uv run infrahubctl branch delete {branch_name}
```

To start fresh:

```bash
# Stop and restart Infrahub
uv run invoke stop
uv run invoke start
```

### Advanced Usage

#### Running Multiple Demos

Deploy multiple use cases to the same branch:

```bash
cd data_center_topology
./deploy.sh --scenario dc3 --branch demo-multi

cd ../server_connectivity
./deploy.sh --scenario dc3 --branch demo-multi

cd ../validation_checks
./deploy.sh --scenario dc3 --branch demo-multi
```

### Custom Data

Modify configurations in each demo's `data/` folder before running `./deploy.sh` to customize the deployment.

### Local Simulation

Some demos support local containerlab simulation:

```bash
cd equinix_pop
./deploy.sh --scenario pop
cd ../..
uv run invoke clab up --pop
```

## Troubleshooting

**Infrahub not running:**

```bash
uv run invoke start
```

**Connection refused:**

```bash
export INFRAHUB_ADDRESS="http://localhost:8000"
export INFRAHUB_API_TOKEN="06438eb2-8019-4776-878c-0941b1f1d1ec"
```

**Schema load errors:**

```bash
uv run infrahubctl schema load ../schemas
```

**Data load errors:**

```bash
uv run infrahubctl object load data/ --branch main
```

## Resources

- [Main README](../README.md) - Project overview and philosophy
- [Infrahub Documentation](https://docs.infrahub.app)
- [Project Discussions](https://github.com/t0m3kz/infrahub-demo/discussions/)

## Continuous Learning

Each demo includes:

- Detailed README with architecture explanations
- Example deployment scripts
- Sample data configurations
- Links to related documentation

Start with **Data Center Topology** for fundamentals, then explore other demos for advanced concepts.

## Contributing

Have ideas for new demos or improvements? Open an issue or discussion in the project repository!
