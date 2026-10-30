# Loader API cheat sheet (Minecraft 26.x, Mojang names)

Minecraft 26.1+ ships unobfuscated: all three loaders use Mojang's official names, no Yarn/remapping.
Older versions (<= 1.21.11) are obfuscated and differ substantially; see "1.20.1 / 1.21.1" below.

## Fabric (plugin `net.fabricmc.fabric-loom`, Fabric API)

- Entrypoints declared in `fabric.mod.json` (`main`, `client`); classes implement `ModInitializer` /
  `ClientModInitializer`.
- Registration is direct, at init time:
  ```java
  ResourceKey<Item> key = ResourceKey.create(Registries.ITEM, Identifier.fromNamespaceAndPath(MOD_ID, "x"));
  Item item = Registry.register(BuiltInRegistries.ITEM, key, new Item(new Item.Properties().setId(key)));
  ```
  Blocks: same with `Registries.BLOCK` and `BlockBehaviour.Properties.of().setId(key)`; register a
  `BlockItem` separately. Properties need `setId(key)` before construction (1.21.2+ requirement).
- Creative tabs: `ItemGroupEvents.modifyEntriesEvent(CreativeModeTabs.X).register(e -> e.accept(item))`.
- Dependencies: `implementation "net.fabricmc.fabric-api:fabric-api:<ver>"` (no remapping on 26.x).
- Client-only code goes in `src/client/java` (split source sets).

## NeoForge (plugin `net.neoforged.moddev`)

- Main class annotated `@Mod(MODID)`, constructor `(IEventBus modEventBus, ModContainer container)`.
- `DeferredRegister.Blocks BLOCKS = DeferredRegister.createBlocks(MODID)`;
  `BLOCKS.registerSimpleBlock("name", p -> p.mapColor(...))`;
  `DeferredRegister.Items ITEMS = DeferredRegister.createItems(MODID)`; `ITEMS.registerSimpleBlockItem(...)`.
  Register each DeferredRegister on `modEventBus`.
- Metadata template `src/main/templates/META-INF/neoforge.mods.toml` (expanded by `generateModMetadata`).
- Game events on `NeoForge.EVENT_BUS`; mod lifecycle events on the mod bus.

## Forge (plugin `net.minecraftforge.gradle` 7.x)

- `@Mod(MODID)`, constructor `(FMLJavaModLoadingContext context)`; `context.getModBusGroup()`.
- `DeferredRegister.create(ForgeRegistries.ITEMS, MODID)`; registry objects need
  `new Item.Properties().setId(ITEMS.key("name"))`.
- EventBus 7: per-event buses, e.g. `FMLCommonSetupEvent.getBus(modBusGroup).addListener(...)`,
  `BuildCreativeModeTabContentsEvent.BUS.addListener(...)`; add
  `annotationProcessor 'net.minecraftforge:eventbus-validator:<ver>'`.
- Metadata `src/main/resources/META-INF/mods.toml` (`mandatory=true` dependency style).

## Common compile errors

| Error | Usual cause |
| --- | --- |
| `cannot find symbol ... ResourceLocation` | 26.x renamed it to `Identifier` |
| `Item id not set` at runtime | missing `.setId(key)` on Properties |
| `package net.fabricmc.fabric.api... does not exist` | Fabric API dependency missing |
| `invalid source release: 25` | JDK older than the game's required Java |

## 1.20.1 / 1.21.1 (obfuscated era; MCAT templates verified 2026-10-30)

- **Fabric**: plugin `net.fabricmc.fabric-loom-remap` with `mappings loom.officialMojangMappings()`
  (Mojang names in source, remapped at build); dependencies via `modImplementation`. Loom 1.19 runs
  only on JVM 25 even though the mod targets Java 17 (1.20.1) / 21 (1.21.1). Ids:
  `new ResourceLocation(ns, path)` (1.20.1) vs `ResourceLocation.fromNamespaceAndPath(ns, path)`
  (1.21+). No `Properties.setId(...)` before 1.21.2.
- **NeoForge 1.21.1**: ModDevGradle 2.0.x, `DeferredRegister.Blocks/Items`,
  `registerSimpleBlock(name, BlockBehaviour.Properties.of())`, `registerSimpleBlockItem`.
- **Forge 1.21.1** (ForgeGradle 7, Java 21) and **Forge 1.20.1** (ForgeGradle 6, Java 17, `reobfJar`):
  `DeferredRegister.create(ForgeRegistries.ITEMS, MODID)`, `RegistryObject<T>`, mod bus from the injected
  constructor parameter: `public Main(FMLJavaModLoadingContext context) { context.getModEventBus(); }`.
  Both need `pack.mcmeta` in resources (`pack_format` 15 for 1.20.1, 34 for 1.21.1).
- NeoForge 1.20.1 is the Forge 47.1 fork; MCAT has no separate template for it.
