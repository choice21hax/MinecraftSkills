package {{package}};

import net.fabricmc.api.ModInitializer;
import net.minecraft.resources.Identifier;
//#if examples
import net.minecraft.core.Registry;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.core.registries.Registries;
import net.minecraft.resources.ResourceKey;
import net.minecraft.world.item.BlockItem;
import net.minecraft.world.item.Item;
import net.minecraft.world.level.block.Block;
import net.minecraft.world.level.block.state.BlockBehaviour;
import net.minecraft.world.level.material.MapColor;
//#endif
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class {{main_class}} implements ModInitializer {
	public static final String MOD_ID = "{{mod_id}}";
	public static final Logger LOGGER = LoggerFactory.getLogger(MOD_ID);
//#if examples

	public static final ResourceKey<Block> EXAMPLE_BLOCK_KEY = ResourceKey.create(Registries.BLOCK, id("example_block"));
	public static final Block EXAMPLE_BLOCK = Registry.register(BuiltInRegistries.BLOCK, EXAMPLE_BLOCK_KEY,
			new Block(BlockBehaviour.Properties.of().setId(EXAMPLE_BLOCK_KEY).mapColor(MapColor.STONE).strength(1.5f)));

	public static final ResourceKey<Item> EXAMPLE_BLOCK_ITEM_KEY = ResourceKey.create(Registries.ITEM, id("example_block"));
	public static final Item EXAMPLE_BLOCK_ITEM = Registry.register(BuiltInRegistries.ITEM, EXAMPLE_BLOCK_ITEM_KEY,
			new BlockItem(EXAMPLE_BLOCK, new Item.Properties().setId(EXAMPLE_BLOCK_ITEM_KEY).useBlockDescriptionPrefix()));

	public static final ResourceKey<Item> EXAMPLE_ITEM_KEY = ResourceKey.create(Registries.ITEM, id("example_item"));
	public static final Item EXAMPLE_ITEM = Registry.register(BuiltInRegistries.ITEM, EXAMPLE_ITEM_KEY,
			new Item(new Item.Properties().setId(EXAMPLE_ITEM_KEY)));
//#endif

	@Override
	public void onInitialize() {
		LOGGER.info("{{mod_name}} initialized");
	}

	public static Identifier id(String path) {
		return Identifier.fromNamespaceAndPath(MOD_ID, path);
	}
}
