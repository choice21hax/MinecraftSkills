package {{package}};

import net.fabricmc.api.ModInitializer;
import net.minecraft.resources.ResourceLocation;
//#if examples
import net.minecraft.core.Registry;
import net.minecraft.core.registries.BuiltInRegistries;
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

	public static final Block EXAMPLE_BLOCK = Registry.register(BuiltInRegistries.BLOCK, id("example_block"),
			new Block(BlockBehaviour.Properties.of().mapColor(MapColor.STONE).strength(1.5f)));
	public static final Item EXAMPLE_BLOCK_ITEM = Registry.register(BuiltInRegistries.ITEM, id("example_block"),
			new BlockItem(EXAMPLE_BLOCK, new Item.Properties()));
	public static final Item EXAMPLE_ITEM = Registry.register(BuiltInRegistries.ITEM, id("example_item"),
			new Item(new Item.Properties()));
//#endif

	@Override
	public void onInitialize() {
		LOGGER.info("{{mod_name}} initialized");
	}

	public static ResourceLocation id(String path) {
		return {{resource_location}};
	}
}
